"""Stage 2 — 가설을 '실행 가능한 전략 후보'로 구체화한다 (LLM → 타입드 유전체).

핵심 설계(Alpha_factory 의 LLM↔결정론 경계와 같은 원리):
  **LLM 은 자유형 수식을 쓰지 않는다.** 유전자(Genome 필드)를 고를 뿐이고, 코드는
  결정론적 render() 가 만든다. 이유는 두 가지다.
    1. 자유형 수식은 유전체 역추출이 손실 압축이라, GA 자식이 부모를 복제조차 못 한다
       (2026-07-11 라이브 진단). 타입드 출력이면 LLM 아이디어가 **무손실로** GA 에 들어가
       그대로 교차·변이된다.
    2. 렌더러가 만든 코드만 다루므로 컴파일 불가·금지 연산자 같은 사고가 원천 차단된다.

WQB 알파에서 '전략 세부사항' 은 결국 유전자다:
    delay       = latency (0=당일 체결, 1=익일)
    decay       = 리밸런싱 평활 (클수록 천천히 바꿔 담음 → turnover↓)
    trade_when  = 조건부 진입 (조건 밖에서는 미보유 → turnover 를 직접 절단)
    truncation  = 종목당 비중 상한 (집중 리스크)
    universe    = 유동성/사이즈 유니버스
    neutralization/group_* = 무엇에 대해 중립화할 것인가 (섹터·산업 리스크 제거)
    winsor_std  = 신호 이상치 절단 (MDD 방어)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import alpha_lint, alpha_repair, genome_models as gm
from .ideation import _llm, parse_json_array

LOG = logging.getLogger('genomicwqb.strategy_spec')


FIELDS_SHOWN_PER_FAMILY = 14
"""프롬프트에 노출하는 family 당 필드 수. 전체 팔레트는 200개가 넘어 그대로 실으면
프롬프트가 폭발하고 로컬 모델이 뒤쪽을 무시한다. 앞쪽(curated·검증된 필드)을 보여주되,
LLM 이 목록 밖 필드를 내면 validate_and_build 가 폐기한다."""


def _palette_block() -> str:
    lines = []
    for fam, fields in gm.SHARED_DATASETS.items():
        shown = list(fields)[:FIELDS_SHOWN_PER_FAMILY]
        more = len(fields) - len(shown)
        tail = f'  … 외 {more}개' if more > 0 else ''
        lines.append(f'  {fam}: {", ".join(shown)}{tail}')
    return '\n'.join(lines)


def _synthetic_block() -> str:
    """합성 팩터 설명 — 이름만 보고는 뭔지 알 수 없으므로 의미를 적어 준다."""
    desc = {
        'syn_clv': '종가가 당일 고저 range 어디에 붙었나 [-1,1] — 매수/매도 압력',
        'syn_oc_ret': '시가 대비 종가 수익 — 장중 모멘텀',
        'syn_gap': '전일 종가 대비 시가 — 갭',
        'syn_range': '일중 변동폭/종가 — 변동성 프록시',
        'syn_vwap_dev': 'VWAP 대비 종가 괴리 — 수급 압력',
        'syn_illiq': 'Amihud 비유동성 — 거래량당 가격충격',
        'syn_turn': '거래량/20일 평균거래대금 — 관심도',
    }
    return '\n'.join(f'  {k}: {v}' for k, v in desc.items() if k in gm.SYNTHETIC_FIELDS)


def _system_prompt() -> str:
    return f"""너는 WorldQuant Brain 알파를 '유전자' 로 설계하는 엔지니어다.
수식을 직접 쓰지 마라. 아래 유전자 값만 고르면 시스템이 결정론적으로 수식을 만든다.

[사용 가능한 데이터필드 — 이 목록 밖의 필드를 쓰면 후보가 폐기된다]
{_palette_block()}

[pv 합성 팩터 — 원시 가격필드로는 못 만드는 구조를 유전자 한 칸에 담는다]
{_synthetic_block()}

[유전자]
- family: {' | '.join(gm.BaseGenomeModel.families)}
    model = WQB 의 mdl77/mdl177 팩터 데이터셋 (가치·퀄리티·모멘텀이 이미 계산돼 있다)
- fields: 위 family 목록에서 **서로 다른 3개** (배열)
- transform_a / transform_b / transform_c: {' | '.join(gm.BaseGenomeModel.transforms)}
    transform_c 는 combine=triple 일 때만 3번째 팩터에 발현
- combine: {' | '.join(gm.BaseGenomeModel.combines)}
    spread=차이, sum=합, product=곱, ratio=비율, corr=시계열상관, triple=3팩터합
- sign: 1 또는 -1 (평균회귀 성격이면 대개 -1)
- lookback_a / lookback_b: 1~252 (시계열 창).  lookback_c: 0=자동
- universe: {' | '.join(gm.UNIVERSES)}
- neutralization: {' | '.join(gm.NEUTRALIZATIONS)}
- decay: 0~30  (리밸런싱 평활. 8 이상이면 바깥 스무딩 발현 → turnover 하락)
- decay_style: mean | linear
- truncation: 0.01~0.15 (종목당 비중 상한)
- regime: {' | '.join(gm.REGIME_KINDS)}
    조건 밖에서는 **신호를 0** 으로 만든 뒤 그 위에 중립화·평활을 얹는다.
    range_expand=일중 변동폭 확대 국면만 / range_calm=축소 국면만 /
    vol_high·vol_low=수익률 변동성 / trend_up·trend_down=추세 / volume_surge=거래급증
    ← 이 계정의 역대 최고 알파(Sharpe 3.77)가 range_expand 를 썼다
- hump: {', '.join(str(x) for x in gm.HUMPS)}
    신호가 문턱만큼 안 움직이면 포지션을 유지 → turnover 를 직접 억제 (0=off)
- trade_when: {' | '.join(gm.TRADE_WHEN_KINDS)}
    regime 과 달리 **최종 알파**를 조건 밖에서 미보유(-1)로 만든다
- group_op: {' | '.join(gm.GROUP_OPS)}   (neutralize=그룹중립, rank=그룹내 순위, zscore=그룹내 표준화)
- group_by: {' | '.join(gm.GROUP_BYS)}   (auto=neutralization 을 따름)
- winsor_std: {', '.join(str(x) for x in gm.WINSOR_STDS)}  (0=off. 이상치 절단 → MDD 방어)
- weight_scheme: {' | '.join(gm.WEIGHT_SCHEMES)}  (2팩터 가중. sum/spread 에서만 발현)

[통과 기준]
  로컬 게이트: Sharpe >= 1.25 · Fitness >= 1.0 · Turnover <= 70% · self-corr < 0.7
  실제 제출컷(RC, delay=1): Sharpe >= 1.58 · Fitness >= 1.0
  추가로 2Y Sharpe(최근 2년) 와 구간별 Sharpe(ladder) 도 통과해야 한다
  → 특정 국면·이상치에 얹힌 신호는 전 구간 Sharpe 가 좋아도 여기서 떨어진다.

[⚠ 반드시 알아야 할 것 — Fitness 의 정의]
  Fitness = Sharpe × sqrt(|Returns| / max(Turnover, 0.125))
  즉 **turnover 를 12.5% 아래로 낮춰도 Fitness 는 전혀 오르지 않는다** (분모가 바닥친다).
  이 계정의 현재 병목이 정확히 이것이다: turnover 는 이미 ~3% 인데 Fitness 가 0.6 에
  묶여 있다. 원인은 returns 가 2~4% 에 불과해서다.
  → **수익률(returns)을 올리는 설계**를 하라: 과도한 평활(decay)·과도한 중립화·좁은
    trade_when 은 turnover 를 더 깎을 뿐 Fitness 를 못 올린다. 신호를 선명하게 하고
    truncation 을 키우고 분산이 큰 유니버스(TOP500/TOP200)를 쓰는 쪽이 낫다.

[후보 다양화 규칙]
- 요청받은 개수만큼 후보를 내되, **서로 유전자가 뚜렷이 달라야 한다**.
  같은 신호를 decay 만 바꿔 내는 식의 복제는 금지.
- 최소 1개는 returns 를 겨냥한 공격형(decay 낮음 + truncation 높음 + 작은 유니버스).
- 최소 1개는 regime 이나 hump 를 활용한 후보.
- delay 는 0(당일) 또는 1(익일). **delay=0 이면 fields 는 pv 필드만** 써야 한다.

출력은 **JSON 배열만**. 코드펜스·설명 금지. 각 원소:
{{"why": "<이 후보가 가설을 어떻게 구현하는지 1문장>",
  "delay": 1,
  "genome": {{"family":"fundamental","fields":["equity","debt","assets"],
    "transform_a":"rank","transform_b":"ts_zscore","combine":"ratio","sign":-1,
    "lookback_a":20,"lookback_b":60,"universe":"TOP1000","neutralization":"INDUSTRY",
    "decay":8,"decay_style":"linear","truncation":0.08,"trade_when":"OFF",
    "regime":"OFF","hump":0.0,"transform_c":"ts_zscore","lookback_c":0,
    "group_op":"neutralize","group_by":"auto","winsor_std":4,"weight_scheme":"1:1"}}}}"""


def validate_and_build(raw_genome: dict, *, account_type: str = 'research_consultant',
                       delay=None) -> dict[str, Any] | None:
    """LLM 이 준 유전자 dict → 검증된 {genome, code, settings}. 실패하면 None.

    검증 사슬(하나라도 걸리면 폐기):
      _coerce_genome (유효값 강제) → 모델 불변식(_constrain) → render()
      → alpha_repair.repair (오타/관용구 교정) → alpha_lint (컴파일 불가 차단)
    """
    import random
    if not isinstance(raw_genome, dict):
        return None
    # ⚠ _coerce_genome 은 빠진 유전자를 **기본값으로 채운다**. 그래서 LLM 이 빈 dict 이나
    #   껍데기를 줘도 'rank(add(rank(close),ts_zscore(open,60)))' 같은 멀쩡한 알파가 나온다 —
    #   사용자의 가설과 아무 상관 없는 알파를 그 가설의 후보라고 우기게 된다.
    #   그러니 신호를 정의하는 필수 유전자는 LLM 이 **명시**했을 때만 받는다.
    required = ('family', 'fields', 'transform_a', 'combine')
    missing = [k for k in required if not raw_genome.get(k)]
    if missing:
        LOG.info('spec 폐기: 필수 유전자 누락 %s', missing)
        return None
    g = gm._coerce_genome({**raw_genome, 'model': 'llm-spec'})
    if g is None:
        return None
    # 필드가 팔레트 밖이면(할루시네이션) 폐기 — 조용히 pv 로 갈아끼우면 가설이 증발한다.
    known = {f for fam in gm.SHARED_DATASETS.values() for f in fam}
    if not set(g.fields) <= known:
        LOG.info('spec 폐기: 팔레트 밖 필드 %s', set(g.fields) - known)
        return None
    if len(set(g.fields)) < 3:
        LOG.info('spec 폐기: 필드 중복 %s', g.fields)
        return None
    d = str(delay) if delay is not None else '1'
    if d == '0' and not set(g.fields) <= set(gm.SHARED_DATASETS['pv']):
        LOG.info('spec 폐기: delay=0 인데 non-pv 필드 %s', g.fields)
        return None

    cls = (gm.ResearchConsultantGenomeModel
           if account_type == 'research_consultant' else gm.StandardGenomeModel)
    model = cls(round_num=0, forced_delay=d)
    g = model._constrain(g, random.Random(0))

    code = gm.render(g)
    try:
        fixed, _actions = alpha_repair.repair(code, delay=d)
        if fixed:
            code = fixed
    except Exception:
        pass
    issues = alpha_lint.validate_alpha(code)
    if issues:
        LOG.info('spec 폐기: lint %s — %s', issues, code[:90])
        return None
    return {'genome': dict(g.__dict__), 'code': code,
            'settings': gm.settings(g, d), 'delay': d}


def concretize(hypothesis: dict, evidence: str = '', *, k: int = 4,
               account_type: str = 'research_consultant') -> list[dict[str, Any]]:
    """가설 1개 → 검증 통과한 전략 후보 k개(이하). 실패 시 빈 리스트(fail-open).

    반환 원소: {genome, code, settings, delay, why}
    """
    title = str((hypothesis or {}).get('title') or '').strip()
    if not title:
        return []
    rationale = str(hypothesis.get('rationale') or '')
    fam = str(hypothesis.get('family_hint') or '')
    ev = (evidence or '').strip()

    base_user = (f'[가설] {title}\n'
                 f'[근거·논리] {rationale}\n'
                 + (f'[선호 데이터 패밀리] {fam}\n' if fam else '')
                 + (f'\n[참고 — 수집 근거 발췌]\n{ev[:6000]}\n' if ev else ''))

    out: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    # 로컬 추론모델은 한 번에 여러 객체를 요구하면 1개만 쓰고 멈추는 일이 잦다.
    # 부족하면 '남은 개수만' 다시 요청한다(최대 2라운드) — 매번 k개를 새로 달라고 하면
    # 앞서 받은 것과 같은 후보만 또 온다.
    for attempt in range(2):
        need = k - len(out)
        if need <= 0:
            break
        user = base_user + (
            f'\n이 가설을 구현하는 전략 후보를 **정확히 {need}개** JSON 배열로 내라.'
            + ('\n\n[이미 만든 후보 — 유전자가 뚜렷이 다른 것을 내라]\n'
               + '\n'.join(f'- {c}' for c in seen_codes) if seen_codes else ''))
        raw = _llm([{'role': 'system', 'content': _system_prompt()},
                    {'role': 'user', 'content': user}],
                   temperature=0.6 + 0.2 * attempt)
        items = parse_json_array(raw)
        if not items:
            LOG.warning('가설 "%s" 후보 파싱 실패 (시도 %d, 응답 %d자)',
                        title[:30], attempt + 1, len(raw))
            continue
        for item in items:
            built = validate_and_build(item.get('genome') or {},
                                       account_type=account_type,
                                       delay=item.get('delay'))
            if not built:
                continue
            if built['code'] in seen_codes:
                continue                          # 같은 수식 복제는 후보가 아니다
            seen_codes.add(built['code'])
            built['why'] = str(item.get('why') or '')[:500]
            out.append(built)
            if len(out) >= k:
                break
    if not out:
        LOG.warning('가설 "%s" 의 후보가 모두 폐기됨', title[:30])
    return out
