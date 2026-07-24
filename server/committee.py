"""전략 위원회 — 다중 LLM 에이전트가 서치스페이스를 **스스로** 정한다 (2026-07-23).

배경
----
사람이 서치스페이스를 좁히면(예: "pv 를 줄여라") 창의성이 함께 죽는다는 사장 방침에
따라, 좁히는 판단 자체를 AI 에게 넘긴다. 밴딧 통계·구역별 실측·거절 사유를 근거로
**세 심사역이 독립 제안**을 내고 **의장이 종합**한다 — QuantInSight 위원회(찬반토론)
패턴의 이식:

  1. 착취 심사역 (temperature 0.3) — 실측 보상이 검증된 구역에 슬롯을 몬다.
  2. 탐험 심사역 (temperature 0.9) — 방문이 적은 팔/조합 중 가치 있는 곳을 찍는다.
  3. 탈상관 심사역 (temperature 0.6) — PROD/SELF_CORRELATION 회피 관점에서 중립화·
     resid 결합·저인기 family 를 제안하고, 엘리트 중 **서로 구조가 다른 교차쌍**
     (X,Y 콤보)을 고른다 — 모든 쌍을 시뮬하는 O(n²) 대신 LLM 이 후보를 좁힌다.
  4. 의장 (temperature 0.2) — 세 제안을 종합해 최종 슬롯 정책 JSON 을 확정한다.

산출물: data/committee_policy.json (원자적 쓰기)
  {user_id, round, valid_rounds, explore_slots, slot_settings[], seed_pairs[][], notes, ts}

소비처 (둘 다 fail-open — 정책이 없거나 낡으면 기존 동작 그대로):
  - worker 밴딧 슬롯 블록: slots_from_policy() 가 검증된 슬롯을 채운다.
  - worker 시드 페치: order_seeds_with_pairs() 가 교차쌍을 인접 배치한다
    (genome_models._plan_ga 는 seeds[j], seeds[j+1] 을 교차하므로 인접 = 실제 교차).

LLM 은 전부 로컬(arcllm)이라 토큰 비용이 없다. 킬스위치: IQC_COMMITTEE=0.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import bandit as _bandit
from . import db as _db

LOG = logging.getLogger('genomicwqb.committee')

COMMITTEE_ON = os.environ.get('IQC_COMMITTEE', '1') != '0'
EVERY_N_ROUNDS = int(os.environ.get('IQC_COMMITTEE_EVERY', '12'))
"""몇 라운드마다 위원회를 소집할지. LLM 4회 호출(로컬)이 수 분 걸리므로 라운드마다는
과하고, 밴딧 통계가 유의미하게 변하는 주기(~100 알파)에 맞춘다."""

VALID_ROUNDS_DEFAULT = int(os.environ.get('IQC_COMMITTEE_VALID_ROUNDS', '12'))
_LLM_TIMEOUT_S = int(os.environ.get('IQC_COMMITTEE_LLM_TIMEOUT_S', '600'))

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
POLICY_PATH = os.path.abspath(os.path.join(_THIS_DIR, '..', 'data',
                                           'committee_policy.json'))

_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_POLICY_LOCK = threading.Lock()

# 슬롯 dict 에서 위원회가 정할 수 있는 차원 — bandit.DIMENSIONS 와 1:1.
_SLOT_KEYS = ('universe', 'neutralization', 'decay_bucket', 'family', 'combine')


# ── 정책 파일 IO ──────────────────────────────────────────────────────────────

def _read_policy_file() -> dict:
    try:
        with open(POLICY_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _write_policy_file(data: dict) -> None:
    os.makedirs(os.path.dirname(POLICY_PATH), exist_ok=True)
    tmp = POLICY_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POLICY_PATH)


def active_policy(user_id: int, current_round: int) -> dict | None:
    """현재 유효한 위원회 정책. 없거나 낡았으면 None (호출부는 기존 동작으로)."""
    if not COMMITTEE_ON:
        return None
    with _POLICY_LOCK:
        p = _read_policy_file()
    if not p or int(p.get('user_id') or 0) != int(user_id):
        return None
    try:
        age = int(current_round) - int(p.get('round') or 0)
        valid = int(p.get('valid_rounds') or VALID_ROUNDS_DEFAULT)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > max(1, valid):
        return None
    if not p.get('slot_settings') and not p.get('seed_pairs'):
        return None
    return p


# ── 정책 검증 (LLM 출력은 신뢰하지 않는다) ───────────────────────────────────

def _sanitize_slot(s) -> dict | None:
    """LLM 이 낸 슬롯 하나를 bandit.DIMENSIONS 전집합으로 검증. 무효 값이 하나라도
    있으면 그 축만 버리고 나머지는 살린다(축이 2개 미만 남으면 슬롯 폐기)."""
    if not isinstance(s, dict):
        return None
    out: dict = {}
    for k in _SLOT_KEYS:
        v = str(s.get(k) or '').strip()
        vals = _bandit.DIMENSIONS.get(k) or []
        if k == 'neutralization':
            v = v.upper()
        if k in ('family', 'combine', 'decay_bucket'):
            v = v.lower()
        if k == 'universe':
            v = v.upper()
        if v in vals:
            out[k] = v
    if len(out) < 2:
        return None
    return out


def _complete_slot(partial: dict, stats: dict, rng) -> dict:
    """부분 슬롯(일부 축만 지정)을 epsilon-greedy 로 채워 완전한 배정으로 만든다."""
    base = _bandit.select_slots(stats, n_slots=1, epsilon=0.2, explore_slots=0,
                                rng=rng)[0]
    base.update(partial)
    base['decay'] = _bandit.DECAY_BUCKET_VALUE.get(
        base.get('decay_bucket') or 'low', 1)
    return base


def sanitize_policy(raw: dict, *, user_id: int, round_num: int) -> dict | None:
    """의장 출력 → 저장 가능한 정책. 슬롯 0개 + 쌍 0개면 None."""
    if not isinstance(raw, dict):
        return None
    slots = []
    for s in (raw.get('slot_settings') or raw.get('slots') or [])[:8]:
        ok = _sanitize_slot(s)
        if ok:
            slots.append(ok)
    pairs = []
    for p in (raw.get('seed_pairs') or [])[:3]:
        try:
            a, b = int(p[0]), int(p[1])
            if a > 0 and b > 0 and a != b:
                pairs.append([a, b])
        except (TypeError, ValueError, IndexError):
            continue
    if not slots and not pairs:
        return None
    try:
        explore = max(0, min(4, int(raw.get('explore_slots'))))
    except (TypeError, ValueError):
        explore = 2
    try:
        valid = max(4, min(48, int(raw.get('valid_rounds'))))
    except (TypeError, ValueError):
        valid = VALID_ROUNDS_DEFAULT
    return {
        'user_id': int(user_id),
        'round': int(round_num),
        'valid_rounds': valid,
        'explore_slots': explore,
        'slot_settings': slots,
        'seed_pairs': pairs,
        'notes': str(raw.get('notes') or '')[:500],
        'ts': time.time(),
    }


# ── 소비 헬퍼 (worker 가 부른다) ─────────────────────────────────────────────

def slots_from_policy(policy: dict, *, n_slots: int, stats: dict, rng) -> list[dict]:
    """정책 슬롯을 앞에 놓고, 남는 슬롯은 (탐험 몫 + epsilon-greedy)로 채운다.

    반환 형식은 bandit.select_slots 와 동일 — 호출부/보상 귀속 코드는 무변경.
    """
    out: list[dict] = []
    for s in (policy.get('slot_settings') or [])[:n_slots]:
        try:
            out.append(_complete_slot(dict(s), stats, rng))
        except Exception:
            continue
    remaining = max(0, n_slots - len(out))
    if remaining:
        explore = min(int(policy.get('explore_slots') or 2), remaining)
        out.extend(_bandit.select_slots(stats, n_slots=remaining, epsilon=0.2,
                                        explore_slots=explore, rng=rng))
    return out[:n_slots]


def order_seeds_with_pairs(pool: list[dict], policy: dict,
                           fallback: list[dict], *, cap: int = 6) -> list[dict]:
    """위원회 교차쌍(alpha id 쌍)을 시드 리스트의 **인접 앞자리**로 배치한다.

    genome_models._plan_ga 의 교차는 seeds[j] × seeds[j+1] 이므로 인접 배치가 곧
    '그 쌍을 교차하라' 는 뜻이 된다. 쌍의 id 가 풀에 없으면 그 쌍은 무시.
    결과가 fallback 보다 빈약하면 fallback 을 그대로 쓴다 (fail-open).
    """
    try:
        by_id = {int(d.get('id') or 0): d for d in (pool or []) if d.get('id')}
        ordered: list[dict] = []
        seen: set[int] = set()
        for a, b in (policy.get('seed_pairs') or []):
            da, db_ = by_id.get(int(a)), by_id.get(int(b))
            if da is None or db_ is None:
                continue
            for d in (da, db_):
                i = int(d.get('id') or 0)
                if i not in seen:
                    ordered.append(d)
                    seen.add(i)
        for d in (fallback or []):
            i = int(d.get('id') or 0)
            if i and i not in seen and len(ordered) < cap:
                ordered.append(d)
                seen.add(i)
        return ordered[:cap] if len(ordered) >= 2 else list(fallback or [])
    except Exception:
        return list(fallback or [])


# ── 근거 블록 ────────────────────────────────────────────────────────────────

def build_evidence(user_id: int) -> str:
    """위원회가 읽을 근거 — 밴딧 arm 통계·구역 실측·거절 사유·엘리트."""
    parts: list[str] = []
    try:
        rows = _db.bandit_stats(user_id)
        if rows:
            lines = [f"  {r['arm_key']}: 방문 {r.get('visits', r.get('n', '?'))} · "
                     f"평균보상 {float(r.get('mean') or 0.0):.4f}"
                     for r in sorted(rows, key=lambda x: -(float(x.get('mean') or 0.0)))]
            parts.append('[밴딧 arm 통계 — 평균보상 내림차순. 방문이 적은 팔은 추정이 '
                         '불확실하다]\n' + '\n'.join(lines[:40]))
    except Exception as e:
        LOG.warning('bandit 근거 실패: %s', e)
    try:
        pockets = _db.pocket_stats(user_id, days=7)
        if pockets:
            lines = [f"  delay={p['delay']} {p['universe']}×{p['neutralization']}: "
                     f"n={p['n']} 평균Sharpe {p['avg_sharpe']} · |S|≥1.25 {p['hi']}건"
                     for p in pockets[:15]]
            parts.append('[최근 7일 구역별 실측 (시뮬 결과)]\n' + '\n'.join(lines))
    except Exception as e:
        LOG.warning('pocket 근거 실패: %s', e)
    try:
        rej = _db.rejection_stats(user_id, days=7)
        if rej:
            lines = [f'  {k}: {v}건' for k, v in rej.items()]
            parts.append('[최근 7일 제출 거절/보류 사유]\n' + '\n'.join(lines))
    except Exception as e:
        LOG.warning('rejection 근거 실패: %s', e)
    try:
        seeds = _db.elite_seeds(user_id, top_n=10)
        if seeds:
            lines = []
            for s in seeds:
                g = s.get('genome') or {}
                m = s.get('metrics') or {}
                lines.append(
                    f"  id={s.get('id')} Sharpe {m.get('sharpe', '?')} "
                    f"Fitness {m.get('fitness', '?')} Turnover {m.get('turnover', '?')} "
                    f"| family={g.get('family')} fields={list(g.get('fields') or [])[:3]} "
                    f"combine={g.get('combine')} {g.get('universe')}×{g.get('neutralization')}")
            parts.append('[엘리트 시드 풀 (id 는 교차쌍 지정에 쓴다)]\n' + '\n'.join(lines))
    except Exception as e:
        LOG.warning('elite 근거 실패: %s', e)
    return '\n\n'.join(parts)


# ── LLM 심사역 ───────────────────────────────────────────────────────────────

_DIMS_DESC = """선택 가능한 값 (이 밖의 값은 무효 처리된다. 각 축에는 반드시 목록의
**단일 값 하나**만 쓴다 — 'FAST/SLOW_AND_FAST' 같은 복합 표기는 통째로 무효):
- universe: {universe}
- neutralization: {neutralization}
- decay_bucket: low(감쇠0~2·고회전) | mid(4·중간) | high(8+·저회전)
- family: {family}
- combine: {combine}   ※ resid = vector_neut(a,b) 잔차 결합 — 탈상관 레버"""


def _dims_desc() -> str:
    d = _bandit.DIMENSIONS
    return _DIMS_DESC.format(
        universe=' | '.join(d.get('universe') or []),
        neutralization=' | '.join(d.get('neutralization') or []),
        family=' | '.join(d.get('family') or []),
        combine=' | '.join(d.get('combine') or []))


_ROLE_PROMPTS = {
    'exploit': (
        0.3,
        """너는 알파 탐색 위원회의 **착취 심사역**이다. 근거의 실측 보상이 이미 검증된
구역(높은 평균보상 + 충분한 방문)에 시뮬 슬롯을 집중시켜라. 방문이 5회 미만인 팔은
추정이 불확실하니 착취 대상으로 삼지 마라.
출력은 JSON 하나만: {"slots": [<슬롯 dict 최대 5개>], "why": "<2문장>"}
슬롯 dict 키: universe, neutralization, decay_bucket, family, combine (일부만 지정해도 됨)"""),
    'explore': (
        0.9,
        """너는 알파 탐색 위원회의 **탐험 심사역**이다. 방문이 적어 아직 검증 안 된 팔
중에서 '초기 신호가 좋거나 구조적으로 유망한' 조합을 골라라. 이미 많이 파인 조합
(방문 수백 회)은 금지. 남들이 안 가본 곳이 네 존재 이유다.
출력은 JSON 하나만: {"slots": [<슬롯 dict 최대 3개>], "why": "<2문장>"}"""),
    'decorr': (
        0.6,
        """너는 알파 탐색 위원회의 **탈상관 심사역**이다. 제출의 최종 관문은
PROD/SELF_CORRELATION(기존 알파풀과의 상관)이다. 이를 뚫는 관점에서:
1) 슬롯 제안 — 리스크팩터 중립화(STATISTICAL/REVERSION_AND_MOMENTUM 등)·resid 결합·
   저인기 family 를 우선 고려하라.
2) 교차쌍 제안 — 엘리트 시드 풀에서 **서로 family 나 메커니즘이 다른** 두 알파의 id 쌍을
   골라라. 교차(crossover)로 섞으면 상관이 낮은 자식이 나온다. 같은 식의 변주 쌍은 금지.
출력은 JSON 하나만:
{"slots": [<슬롯 dict 최대 3개>], "seed_pairs": [[id,id],...최대 3쌍], "why": "<2문장>"}"""),
}

_CHAIR_PROMPT = """너는 알파 탐색 위원회의 **의장**이다. 세 심사역(착취/탐험/탈상관)의
제안과 근거를 읽고 다음 라운드들의 슬롯 정책을 확정하라.

원칙:
- 슬롯 8개 중 대략 절반은 착취, 2개는 탐험, 나머지는 탈상관에 배분하되, 근거가
  한쪽을 강하게 지지하면 조정해도 된다. 판단의 주인은 너다.
- 탐험 몫(explore_slots)은 0~4. 밴딧 통계가 이미 뚜렷하면 줄이고, 정체 상태면 늘려라.
- seed_pairs 는 탈상관 심사역 제안 중 실제 id 가 있는 것만.

출력은 JSON 하나만 (설명·코드펜스 금지):
{"slot_settings": [<슬롯 dict 최대 8개>],
 "explore_slots": <0~4>,
 "seed_pairs": [[id,id],...],
 "valid_rounds": <이 정책의 유효 라운드 수, 8~24>,
 "notes": "<1~2문장 요약 — 대시보드 로그에 그대로 남는다>"}"""


def _parse_json_object(text: str) -> dict:
    """LLM 응답에서 **최상위 객체 하나**를 뽑는다.

    ⚠ ideation.parse_json_array 를 쓰면 안 된다 — 그 파서는 첫 '[' 를 찾으므로
    `{"slots": [...]}` 응답에서 바깥 객체가 아니라 **내부 slots 배열**을 잡아
    껍데기 키(slots/seed_pairs/why)가 통째로 증발한다 (2026-07-23 라이브 실측:
    심사역 응답이 완벽한 JSON 인데도 '유효한 정책 산출 실패' 가 나온 원인).
    """
    import re as _re
    from .ideation import _balanced_slice
    s = (text or '').strip()
    if not s:
        return {}
    s = _re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', s, flags=_re.MULTILINE).strip()
    i = s.find('{')
    while i >= 0:
        chunk = _balanced_slice(s, i, '{', '}')
        if not chunk:
            break
        try:
            o = json.loads(chunk)
            if isinstance(o, dict):
                return o
        except ValueError:
            pass
        i = s.find('{', i + 1)
    return {}


def _call_role(role: str, evidence: str) -> dict:
    """심사역 한 명 호출 → 파싱된 dict (실패 시 {})."""
    from .ideation import _llm
    temp, system = _ROLE_PROMPTS[role]
    user = f'{_dims_desc()}\n\n[수집 근거]\n{evidence}\n\nJSON 으로 답하라.'
    raw = _llm([{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}],
               max_tokens=24000, temperature=temp, timeout=_LLM_TIMEOUT_S)
    return _parse_json_object(raw)


def _call_chair(evidence: str, proposals: dict) -> dict:
    from .ideation import _llm
    user = (f'{_dims_desc()}\n\n[수집 근거 요약]\n{evidence[:4000]}\n\n'
            f'[심사역 제안]\n{json.dumps(proposals, ensure_ascii=False, indent=1)}\n\n'
            'JSON 으로 최종 정책을 확정하라.')
    raw = _llm([{'role': 'system', 'content': _CHAIR_PROMPT},
                {'role': 'user', 'content': user}],
               max_tokens=24000, temperature=0.2, timeout=_LLM_TIMEOUT_S)
    return _parse_json_object(raw)


# ── 실행 ─────────────────────────────────────────────────────────────────────

def should_run(user_id: int, round_num: int) -> bool:
    if not COMMITTEE_ON or EVERY_N_ROUNDS <= 0:
        return False
    if round_num <= 0 or round_num % EVERY_N_ROUNDS != 0:
        return False
    with _LOCK:
        return user_id not in _RUNNING


def start(user_id: int, round_num: int) -> bool:
    with _LOCK:
        if user_id in _RUNNING:
            return False
        _RUNNING.add(user_id)
    t = threading.Thread(target=_run, args=(user_id, round_num),
                         name=f'iqc-committee-{user_id}', daemon=True)
    t.start()
    return True


def _log(user_id: int, round_num: int, line: str) -> None:
    try:
        _db.append_log(user_id, round_num, line, level='info')
    except Exception:
        pass


def _run(user_id: int, round_num: int) -> None:
    try:
        evidence = build_evidence(user_id)
        if not evidence:
            LOG.info('uid=%s 위원회 근거 없음 — 소집 생략', user_id)
            return
        _log(user_id, round_num,
             '🏛 전략 위원회 소집 — 착취·탐험·탈상관 심사역이 독립 제안 작성 중...')
        proposals: dict = {}
        _role_kr = {'exploit': '착취', 'explore': '탐험', 'decorr': '탈상관'}
        for role in ('exploit', 'explore', 'decorr'):
            try:
                p = _call_role(role, evidence)
            except Exception as e:
                LOG.warning('심사역 %s 실패: %s', role, e)
                p = {}
            if p:
                proposals[role] = p
                _log(user_id, round_num,
                     f"   · {_role_kr[role]} 심사역 — 슬롯 {len(p.get('slots') or [])}개"
                     + (f" · 교차쌍 {len(p.get('seed_pairs') or [])}쌍"
                        if p.get('seed_pairs') else '')
                     + (f" — {str(p.get('why') or '')[:80]}" if p.get('why') else ''))
            else:
                # 무응답의 최빈 원인: 로컬 추론모델이 thinking 만 하고 content 가 빈 경우
                # (max_tokens 부족) 또는 타임아웃. arcllm 재시도 후에도 비면 여기 온다.
                _log(user_id, round_num, f'   · {_role_kr[role]} 심사역 — 무응답/파싱 실패')
        if not proposals:
            _log(user_id, round_num, '⚠ 위원회 — 심사역 전원 무응답 (기존 밴딧 유지)')
            return
        try:
            chair = _call_chair(evidence, proposals)
        except Exception as e:
            LOG.warning('의장 실패: %s', e)
            chair = {}
        if not chair:
            _log(user_id, round_num, '   · 의장 — 무응답/파싱 실패 (심사역 제안 직합으로 폴백)')
        policy = sanitize_policy(chair, user_id=user_id, round_num=round_num)
        if policy is None:
            # 의장이 죽어도 심사역 제안이 있으면 그걸 그대로 합쳐 쓴다 (fail-open 이 아니라
            # fail-soft — 세 명이 일한 결과를 버리지 않는다).
            merged = {'slot_settings': [], 'seed_pairs': []}
            for role in ('exploit', 'decorr', 'explore'):
                merged['slot_settings'].extend((proposals.get(role) or {}).get('slots') or [])
                merged['seed_pairs'].extend((proposals.get(role) or {}).get('seed_pairs') or [])
            policy = sanitize_policy(merged, user_id=user_id, round_num=round_num)
        elif not policy['slot_settings']:
            # 의장의 슬롯 표기가 검증에서 전부 탈락한 경우 (2026-07-23 첫 소집 실측:
            # 'FAST/SLOW_AND_FAST' 같은 복합 표기라 사전집합 매칭 실패 → 슬롯 0개).
            # 심사역 원제안은 정상 파싱됐으므로 그쪽 슬롯으로 보강한다.
            merged_slots: list = []
            for role in ('exploit', 'decorr', 'explore'):
                merged_slots.extend((proposals.get(role) or {}).get('slots') or [])
            extra = sanitize_policy({'slot_settings': merged_slots},
                                    user_id=user_id, round_num=round_num)
            if extra and extra['slot_settings']:
                policy['slot_settings'] = extra['slot_settings'][:8]
                _log(user_id, round_num,
                     '   · 의장 슬롯 전원 검증 탈락 — 심사역 원제안 슬롯 '
                     f"{len(policy['slot_settings'])}개로 보강")
        if policy is None:
            _log(user_id, round_num, '⚠ 위원회 — 유효한 정책 산출 실패 (기존 밴딧 유지)')
            return
        with _POLICY_LOCK:
            _write_policy_file(policy)
        n_slot = len(policy['slot_settings'])
        n_pair = len(policy['seed_pairs'])
        _log(user_id, round_num,
             f"✅ 위원회 정책 확정 — 슬롯 {n_slot}개 · 탐험 {policy['explore_slots']} · "
             f"교차쌍 {n_pair}쌍 · {policy['valid_rounds']}라운드 유효"
             + (f" · {policy['notes']}" if policy.get('notes') else ''))
    except Exception as e:
        LOG.exception('위원회 실패')
        _log(user_id, round_num, f'⚠ 위원회 예외(무시): {e}')
    finally:
        with _LOCK:
            _RUNNING.discard(user_id)
