"""submit_push — 하루 제출 목표를 말 안 해도 스스로 채우는 루틴.

2026-08-02 사장 지시("급하게 찾아달라 하지 않아도 알아서")의 코드화. 그날
수동으로 이긴 플레이를 그대로 자동화한다:

  · 검증 골격 시딩 — vector_neut(레벨 zscore(축), zscore(indmom)) + CROWDING.
    제출 성공 2회(α#34550 anlystsn, α#35227 season)의 그 골격이다.
  · 축 소진 추적 — 제출(OS)된 코드에 쓰인 축은 형제들의 PROD_CORRELATION 이
    수 시간 안에 0.75+ 로 급등해(실측 α#34879) 재사용이 불가능하다.
  · 죽은 축 추적 — 이 골격으로 시도해 S<1 이면 다시 쏘지 않는다.
  · 부호는 모른다 — 축당 ±두 발 (웨이브1에서 부호 추측이 최대 리스크였다).

worker 가 라운드 시작 시 maybe_seed() 를 호출하면 스펙이 기존 verbatim 최우선
경로(pending_specs)로 흘러가고, 통과 시 기존 게이트가 자동 제출한다.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import time as _time

from . import db as _db
from .pace_keeper import DAY_BOUNDARY_HOUR

LOG = logging.getLogger('genomicwqb.submit_push')

# 목표 = 일일 쿼터 전부 (2026-08-03 사장: "최대한 많이 찾아서 전부 제출").
# 예산 초과 시도는 게이트가 대기 큐로 돌리니 과장전 리스크는 없다.
DAILY_SUBMIT_TARGET = int(os.environ.get('IQC_PUSH_DAILY_SUBMITS', '4'))
# 집중 창 — 경계(13:00) 직전 3시간(=10:00부터) 동안만, 웨이브를 라운드마다 연사한다
# (2026-08-02 사장: "기준 오전 10시로 해서 3시간동안 빡세게"). 쿨다운은 겹장전
# 방지용 바닥값일 뿐, 실제 페이싱은 라운드 주기(15~25분)가 맡는다.
PUSH_WINDOW_S = float(os.environ.get('IQC_PUSH_WINDOW_S', str(3 * 3600)))
COOLDOWN_S = float(os.environ.get('IQC_PUSH_COOLDOWN_S', '600'))
DEAD_AXIS_SHARPE = 1.0        # 이 골격 시도의 최고 S 가 이 미만이면 죽은 축
#: 발굴이 노리는 커뮤니티 사용량 구간 (2026-08-17 실측).
#: 사용 0~1 필드 24종을 쏴 봤더니 **전부 샤프 0.5 이하**였다 — 아무도 안 쓰는 건
#: 안 붐비는 게 아니라 신호가 없는 것이었다. 반대로 3000 이상은 실제로 붐빈다
#: (rsk70 계열 상관 0.84). 커뮤니티가 검증했지만 아직 포화는 아닌 구간을 판다.
SIGNAL_USAGE_MIN = float(os.environ.get('IQC_SIGNAL_USAGE_MIN', '120'))
SIGNAL_USAGE_MAX = float(os.environ.get('IQC_SIGNAL_USAGE_MAX', '3000'))
AXES_PER_WAVE = 4             # 축 4 × 부호 2 = 스펙 8 (라운드 limit)
# 창 밖 상시 다변화 웨이브 (2026-08-04 사장 지시) — Power Pool 점수는 개수가 아니라
# 풀에 더한 순증분이라, 같은 데이터셋(rsk70) 형제를 아무리 쌓아도 1개어치다. 창 밖에선
# **카탈로그 발굴 축(=다른 데이터셋) 전용**으로 소형 웨이브만 돌린다. 크기가 작고
# 쿨다운이 긴 이유는 스펙이 있는 라운드는 focus(연마)·밴딧·재조합이 전부 꺼지기 때문
# (worker.is_spec_round). 45분 ≈ 라운드 2~3회에 1번만 양보한다.
OFF_WINDOW_AXES = int(os.environ.get('IQC_PUSH_OFF_AXES', '1'))
OFF_WINDOW_COOLDOWN_S = float(os.environ.get('IQC_PUSH_OFF_COOLDOWN_S', '2700'))
TITLE = '자동 제출 푸시'

# ── prod 상관 완화 레인 (2026-08-14 사장 지시) ────────────────────────────────
# 부트캠프 5주차 조언: "0.9 가 넘는 알파를 바꿔서 prod 상관을 줄이려는 건 그냥 어렵다,
# 그건 버려야 한다. 0.8 정도(0.79)면 어느 정도 가능성이 있으니 neutralization 스윕을
# 해보거나 vector_neut·regression_neut 에 다른 알파를 넣어 봐라."
# 우리 실측(2026-08-14)도 같은 그림이었다 — 발사 134건의 prod 상관 중앙값 0.9028,
# **최소가 0.7111 로 0.7 아래가 한 건도 없었다.** 구제할 값어치는 문턱 바로 위에만 있다.
RESCUE_TITLE = 'prod 상관 완화'
PROD_RESCUE_LO = float(os.environ.get('IQC_PROD_RESCUE_LO', '0.70'))
PROD_RESCUE_HI = float(os.environ.get('IQC_PROD_RESCUE_HI', '0.80'))
RESCUE_MAX_ALPHAS = int(os.environ.get('IQC_PROD_RESCUE_ALPHAS', '2'))
RESCUE_COOLDOWN_S = float(os.environ.get('IQC_PROD_RESCUE_COOLDOWN_S', '1800'))
# 손잡이는 둘뿐이다. decay·truncation·winsorize 는 "알파를 마지막에 살짝 튜닝하는 값이지
# 알파 자체를 바꾸는 연산이 아니라"(5주차) 상관을 못 움직인다 — 유전자로 넣지 않는다.
RESCUE_NEUTS = ('SUBINDUSTRY', 'INDUSTRY', 'SECTOR', 'MARKET', 'STATISTICAL', 'CROWDING')
RESCUE_CODE_MAX = 900          # 직교화는 식 둘을 겹치므로 길이를 본다
_PROD_RX = re.compile(r'PROD_CORRELATION\(\s*([\d.]+)\s+vs|prod_corr\(\s*([\d.]+)\s*>')


def _json_genome(raw):
    """alphas.genome 은 JSON 문자열로 저장된다. 못 읽으면 None — 그 알파는 건너뛴다."""
    if isinstance(raw, dict):
        return raw
    try:
        import json as _json
        v = _json.loads(str(raw or '') or 'null')
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def prod_corr_of(status) -> float | None:
    """거절 사유 문자열에서 prod 상관 실값. 403 본문과 발사 전 가드 두 형식을 다 읽는다."""
    m = _PROD_RX.search(str(status or ''))
    if not m:
        return None
    try:
        return float(m.group(1) or m.group(2))
    except (TypeError, ValueError):
        return None


def maybe_rescue(user_id: int, log_fn=None, now: float | None = None) -> int:
    """문턱 바로 위(0.70~0.80)에서 막힌 알파의 **중립화를 스윕해** 재장전한다.

    0.80 이상은 손대지 않는다 — 되돌아오지 않는다는 게 강의 조언이자 우리 실측이다.

    ⚠ 원본 유전체가 있어야 한다. 스펙 경로(worker → spec_genomes)는 `code` 컬럼이 아니라
      **유전체를 다시 render 해서** 시뮬하므로, 유전체로 표현 못 하는 식(예: 알파 둘을
      vector_neut 로 겹치는 직교화)은 조용히 기본 유전체로 뭉개진다(2026-08-14 실측 —
      raw code 13개가 후보 1개로 붕괴). 직교화는 verbatim 경로가 생긴 뒤에 붙인다.
    반환: 주입한 스펙 수.
    """
    now = _time.time() if now is None else now
    if _db.pending_specs(user_id, limit=1):
        return 0                                   # 탄약 소화 중 — 겹장전 금지
    last = _db.last_hypothesis_ts(user_id, RESCUE_TITLE)
    if last and now - last < RESCUE_COOLDOWN_S:
        return 0
    try:
        rows = _db.prod_corr_rejected(user_id, now - 3 * 86400)
    except Exception as e:
        LOG.warning('상관 완화 후보 조회 실패: %s', e)
        return 0

    from . import alpha_lint, genome_models
    try:
        allowed = set(genome_models._allowed_neutralizations())
    except Exception:
        allowed = set(RESCUE_NEUTS)

    seen, targets, no_genome = set(), [], 0
    for r in rows:
        pc = prod_corr_of(r['status'])
        code = str(r['code'] or '')
        if pc is None or not (PROD_RESCUE_LO <= pc < PROD_RESCUE_HI) or code in seen:
            continue
        g = genome_models._coerce_genome(_json_genome(r['genome']))
        if g is None:
            no_genome += 1
            continue
        seen.add(code)
        targets.append((pc, r, g))
    if not targets:
        if no_genome and log_fn:
            log_fn(f'🧬 {RESCUE_TITLE} — 문턱 근접 {no_genome}건이 원본 유전체가 없어 건너뜀')
        return 0

    hid, n, used = None, 0, []
    for pc, r, g in targets:
        if len(used) >= RESCUE_MAX_ALPHAS:
            break
        cur = str(getattr(g, 'neutralization', '') or '').upper()
        fresh = 0
        for nz in RESCUE_NEUTS:
            if nz == cur or nz not in allowed:
                continue
            d = dict(g.__dict__)
            d['neutralization'] = nz
            d['generation'] = int(d.get('generation') or 0) + 1
            try:
                child = genome_models._coerce_genome(d)
                code = genome_models.render(child)
                if child is None or alpha_lint.validate_alpha(code):
                    continue
            except Exception as e:
                LOG.warning('상관 완화 변주 렌더 실패 %s: %s', nz, e)
                continue
            if _db.get_alpha_by_code(user_id, code):
                continue        # 이미 돌려본 변주 — 부모는 계속 거절 상태라 웨이브마다 재탕된다
            if hid is None:
                hid = _db.insert_hypothesis(
                    _db.latest_run_id(user_id), user_id,
                    {'title': f'{RESCUE_TITLE} {_time.strftime("%Y-%m-%d", _time.localtime(now))}',
                     'rationale': 'PROD_CORRELATION 이 컷 바로 위에서 막힌 알파의 중립화를 훑는다. '
                                  '0.80 이상은 되돌아오지 않으므로 제외(부트캠프 5주차 조언·자체 실측).'})
            _db.insert_spec(
                hid, user_id, genome=dict(child.__dict__), code=code,
                settings={'universe': child.universe, 'neutralization': nz,
                          'decay': str(child.decay), 'truncation': str(child.truncation),
                          'nan_handling': child.nan_handling, 'delay': str(r['delay'] or 1)},
                delay=r['delay'], why=f'prod {pc:.4f} → 중립화 {cur or "?"}→{nz}')
            n += 1
            fresh += 1
        if fresh:
            used.append(pc)
    if n and log_fn:
        log_fn(f'🧬 {RESCUE_TITLE} — 문턱 근접 {len(used)}건'
               f'(prod {", ".join(f"{p:.3f}" for p in used)}) 중립화 스윕 스펙 {n}개 장전')
    return n

NEUT_FIELD = 'rsk70_mfm2_gemtrd_indmom'
# 신호 축 후보 — 이상현상 성격(계절성·리버설·저변동성) 우선, 고전 팩터는 뒤로.
# indmom(중립축)·primaryindustry(범주형) 제외. 소진/사망은 DB 실측으로 걸러진다.
AXES = (
    'rsk70_mfm2_gemtrd_season', 'rsk70_mfm2_gemtrd_strevrsl',
    'rsk70_mfm2_gemtrd_ltrevrsl', 'rsk70_mfm2_gemtrd_srisku',
    'rsk70_mfm2_gemtrd_resvol', 'rsk70_mfm2_gemtrd_earnvar',
    'rsk70_mfm2_gemtrd_divyild', 'rsk70_mfm2_gemtrd_midcap',
    'rsk70_mfm2_gemtrd_dsrt', 'rsk70_mfm2_gemtrd_anlystsn',
    'rsk70_mfm2_gemtrd_newssent', 'rsk70_mfm2_gemtrd_shortint',
    'rsk70_mfm2_gemtrd_earnqlty', 'rsk70_mfm2_gemtrd_growth',
    'rsk70_mfm2_gemtrd_profit', 'rsk70_mfm2_gemtrd_invsqlty',
    'rsk70_mfm2_gemtrd_earnyild', 'rsk70_mfm2_gemtrd_leverage',
    'rsk70_mfm2_gemtrd_srisk', 'rsk70_mfm2_gemtrd_btop',
    'rsk70_mfm2_gemtrd_srtindcnt', 'rsk70_mfm2_gemtrd_cntadj',
    'rsk70_mfm2_gemtrd_indadj', 'rsk70_mfm2_gemtrd_beta',
    'rsk70_mfm2_gemtrd_world', 'rsk70_mfm2_gemtrd_size',
)

BASE_GENOME = {   # α#34550/35227 골격 원본
    'model': 'rc-api-genome', 'family': 'model',
    'transform_a': 'ts_zscore', 'transform_b': 'ts_zscore', 'transform_c': 'ts_zscore',
    'combine': 'resid', 'lookback_a': 20, 'lookback_b': 20,
    'universe': 'TOPDIV3000', 'neutralization': 'CROWDING', 'decay': 4,
    'truncation': 0.08, 'nan_handling': 'OFF', 'decay_style': 'mean',
    'trade_when': 'OFF', 'regime': 'OFF', 'group_op': 'neutralize',
    'group_by': 'auto', 'winsor_std': 4, 'weight_scheme': '1:1',
}


def _day0(now: float) -> float:
    lt = _time.localtime(now)
    d0 = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                       DAY_BOUNDARY_HOUR, 0, 0, 0, 0, -1))
    return d0 if d0 <= now else d0 - 86400.0


def _in_window(now: float) -> bool:
    """다음 경계(13:00) 직전 PUSH_WINDOW_S(3h) 안인가 — 집중 장전 창."""
    nxt = _day0(now) + 86400.0
    return nxt - PUSH_WINDOW_S <= now < nxt


def _scope() -> tuple[str, str, str]:
    """활성 조건의 (region, universe, delay) — 없으면 GLB/TOPDIV3000/D1."""
    try:
        from . import run_config
        c = run_config.get_constraint()
        return ((str(getattr(c, 'region', '') or '') or 'GLB').upper(),
                (str(getattr(c, 'universe', '') or '') or 'TOPDIV3000').upper(),
                str(getattr(c, 'delay', '') or '1'))
    except Exception:
        return 'GLB', 'TOPDIV3000', '1'


def _short_cats(user_id: int) -> dict[str, int]:
    """이 스코프에서 아직 3건이 안 찬 피라미드 카테고리 → 남은 발 수."""
    try:
        from . import pyramids as _pyr
        region, _u, delay = _scope()
        return _pyr.shortfall(user_id, region, delay)
    except Exception as e:
        LOG.warning('피라미드 미달 조회 실패(다변화 우선순위 없음): %s', e)
        return {}


def _same_dataset_partners(field: str, rows: list[dict]) -> tuple[str, str] | None:
    """같은 데이터셋 안의 중립축·보조축 2개 (커버리지 높은 순).

    골격에 rsk70 을 섞으면 코드가 데이터셋 둘을 쓰게 되고, WQB 는 그런 알파를
    risk 칸으로 셈한다 — 다변화가 그 자리에서 무효가 된다. 실측(2026-08-13):
    데이터셋 하나만 쓰는 코드 4,865건은 피라미드 칸이 모순 0 으로 갈렸고,
    둘 이상 섞인 코드는 어느 쪽 공로인지 판별 자체가 안 됐다.
    """
    from . import datafield_palette as _pal
    ds = str(_pal.field_dataset_map().get(field) or '').strip().lower()
    if not ds:
        return None
    same = []
    for r in rows:
        name = str(r.get('name') or '')
        if name == field or str(r.get('category') or '').strip().lower() != ds:
            continue
        try:
            same.append((-float(r.get('coverage') or 0), name))
        except (TypeError, ValueError):
            continue
    same.sort()
    return (same[0][1], same[1][1]) if len(same) >= 2 else None


def _cross_dataset_partners(field: str, rows: list[dict]) -> tuple[str, str] | None:
    """다른 데이터셋에서 짝 2개 — 같은 데이터셋에 짝이 없을 때의 폴백.

    한 재료로는 제출 조건 다섯 개를 동시에 못 만족시킨다(2026-08-17~18 실측 300여 건):
    EMEA 샤프가 서는 데이터셋은 IS_LADDER 가 안 서고, 래더가 서는 ML 예측 계열은
    EMEA 가 0.03 이다. 통과한 알파 3건은 전부 그 둘을 **가로질러** 합성한 것이다.

    커버리지가 높고 커뮤니티 사용이 중간대인 필드를 고른다 — 사용 0~수십인 필드는
    안 붐비는 게 아니라 신호가 없었다(08-17 실측: 그런 필드 24종 전부 샤프 0.5 이하).
    """
    from . import datafield_palette as _pal
    ds = str(_pal.field_dataset_map().get(field) or '').strip().lower()
    out = []
    for r in rows:
        name = str(r.get('name') or '')
        if not name or name == field:
            continue
        if str(r.get('category') or '').strip().lower() == ds:
            continue                      # 같은 데이터셋은 위에서 이미 시도했다
        try:
            cov = float(r.get('coverage') or 0)
            used = float(r.get('alphas') or 0)
        except (TypeError, ValueError):
            continue
        if cov < 95 or not (SIGNAL_USAGE_MIN <= used <= SIGNAL_USAGE_MAX):
            continue
        out.append((-cov, used, name))
    if len(out) < 2:
        return None
    out.sort()
    return (out[0][2], out[1][2])


def _discover_axes(exclude: set, n: int, user_id: int | None = None) -> list[str]:
    """카탈로그(live_datafields)에서 새 신호 축을 스스로 발굴한다 — 축이 고갈되면
    사람을 부르는 게 아니라 여기로 온다(2026-08-02 사장: "알아서 찾아야지").
    Matrix형·현재 리전/유니버스/delay 일치·커버리지 60%+ 중에서 커뮤니티 사용
    (alphas)이 적은 순 — 저사용 = 덜 crowded = PROD_CORR 가 낮을 확률.
    금지 데이터셋/무효 필드는 렌더 후 alpha_lint 가 걸러 준다."""
    try:
        from . import datafield_palette as _pal
        rows = _pal._all_rows()
    except Exception as e:
        LOG.warning('축 발굴 실패 — 카탈로그 읽기: %s', e)
        return []
    region, universe, delay = _scope()
    short = _short_cats(user_id) if user_id else {}
    from . import pyramids as _pyr
    cands = []
    for r in rows:
        name = str(r.get('name') or '')
        if (not name or name in exclude
                or str(r.get('type', '')).lower() != 'matrix'
                or str(r.get('region', '')).upper() != region
                or str(r.get('universe', '')).upper() != universe
                or str(r.get('delay', '')) != delay):
            continue
        try:
            cov = float(r.get('coverage') or 0)
            if cov < 60:
                continue
        except (TypeError, ValueError):
            continue
        try:
            used = int(float(r.get('alphas') or 0))
        except (TypeError, ValueError):
            used = 0
        # 사용량 구간 밖은 버린다 — 아래 정렬이 '저사용 우선' 이라 이 필터가 없으면
        # 신호 없는 필드(사용 0~1)가 매 웨이브를 통째로 차지한다(2026-08-17 실측).
        if not (SIGNAL_USAGE_MIN <= used <= SIGNAL_USAGE_MAX):
            continue
        # 🔺 미달 피라미드 칸이 1순위 (2026-08-13 사장 결정). 이미 3건을 채운 칸에
        #    덧쌓으면 피라미드는 그대로인데 PROD_CORRELATION 만 올라 그 계보가
        #    통째로 제출 불능이 된다 — 그날 12/12 거절이 그 결과였다.
        cat = _pyr.dataset_category(str(r.get('category') or ''))
        rank = 0 if (cat and cat in short) else 1
        cands.append((rank, used, -cov, name, cat))   # 미달칸 → 저사용(구간 안) → 고커버리지
    cands.sort()
    # 한 칸에 필요한 만큼만 뽑는다. 안 그러면 한 웨이브가 통째로 같은 데이터셋으로
    # 채워져 방금 고친 병(한 칸에 16발)을 그대로 재현한다 — 실측: 미달 칸 우선만
    # 켰더니 12개 중 10개가 macro_equity_signals 하나였다.
    out, taken = [], collections.Counter()
    for _rank, _used, _cov, name, cat in cands:
        if cat and taken[cat] >= short.get(cat, n):
            continue
        taken[cat] += 1
        out.append(name)
        if len(out) >= n:
            break
    return out


def _axis_stats(user_id: int) -> tuple[set, dict, str]:
    """(소진 축, 축별 최고 S, 최근 코드 전문) — 최근 30일 코드 실측.
    '시도'는 이 골격(indmom 대비 잔차)에서 신호 쪽에 쓰인 경우만 센다.
    세 번째 값은 발굴 축 중복 제거용 — 큐레이션 축과 달리 발굴 축은 소진/사망
    테이블이 없어서, 이미 코드에 등장한 적 있으면 다시 쏘지 않는다."""
    burned, best, seen = set(), {}, []
    for code, sharpe, submitted in _db.code_sharpe_submitted_since(
            user_id, _time.time() - 30 * 86400):
        seen.append(code)
        ipos = code.find(NEUT_FIELD)
        for f in AXES:
            if f not in code:
                continue
            if submitted:
                burned.add(f)
            if ipos > 0 and code.find(f) < ipos:
                try:
                    best[f] = max(best.get(f, -9.0), float(sharpe))
                except (TypeError, ValueError):
                    pass
    return burned, best, '\n'.join(seen)


def maybe_seed(user_id: int, log_fn=None, now: float | None = None) -> int:
    """라운드 시작 훅 — 집중 창(10:00~13:00) 동안 오늘 제출이 목표 미달이면
    신선 축 스펙을 라운드마다 장전한다. 큐레이션 축이 마르면 카탈로그에서 발굴.
    반환: 주입한 스펙 수. 어떤 실패도 라운드를 막으면 안 된다(호출측 try/except)."""
    now = _time.time() if now is None else now
    focused = _in_window(now)
    n_axes = AXES_PER_WAVE if focused else OFF_WINDOW_AXES
    done = _db.submitted_count_since(user_id, _day0(now))
    if done >= DAILY_SUBMIT_TARGET:
        return 0
    # 문턱 바로 위에서 막힌 알파가 있으면 그게 신규 축보다 제출에 가깝다 — 먼저 쏜다.
    rescued = maybe_rescue(user_id, log_fn=log_fn, now=now)
    if rescued:
        return rescued
    if _db.pending_specs(user_id, limit=1):
        return 0                                  # 탄약 소화 중 — 겹장전 금지
    last = _db.last_hypothesis_ts(user_id, TITLE)
    if last and now - last < (COOLDOWN_S if focused else OFF_WINDOW_COOLDOWN_S):
        return 0
    burned, best, seen = _axis_stats(user_id)
    short = _short_cats(user_id)
    # 창 안 = 검증된 큐레이션 축부터, 창 밖 = 다른 데이터셋 전용(발굴만).
    # 단, 큐레이션 축(전부 risk70)은 **RISK 칸이 아직 미달일 때만** 쓴다. 칸이 차면
    # 같은 골격을 더 쏴도 피라미드는 안 늘고 상관만 오른다. 분기가 바뀌어 칸이
    # 다시 비면 검증된 골격이 저절로 돌아온다 — 손으로 켜고 끄지 않는다.
    cands = ([f for f in AXES if f not in burned and best.get(f, 9.9) >= DEAD_AXIS_SHARPE]
             if focused and 'RISK' in short else [])
    if len(cands) < n_axes:
        k = n_axes - len(cands)
        tried = burned | set(best) | set(AXES)
        # 넉넉히 받아 '이미 써본 필드'를 걸러낸다.
        # ponytail: 필드명 substring 검사 — 이름이 길어 충돌 없음, 틀리면 이름 집합 추출로.
        found = [f for f in _discover_axes(tried, k * 4, user_id) if f not in seen][:k]
        if found and log_fn:
            log_fn(f'🔭 축 발굴 — 카탈로그에서 신규 {len(found)}개: '
                   + ', '.join(found))
        cands += found
    if not cands:
        if focused and log_fn:
            log_fn('🚨 자동 제출 푸시 — 큐레이션·카탈로그 모두 축이 고갈됐습니다 '
                   '(발굴 필터를 점검해 주세요).')
        return 0                                  # 창 밖은 조용히 넘어간다

    from . import alpha_lint, genome_models
    try:
        from . import run_config
        spec_c = run_config.get_constraint()
        universe = str(getattr(spec_c, 'universe', '') or '') or BASE_GENOME['universe']
        delay = str(getattr(spec_c, 'delay', '') or '1')
    except Exception:
        universe, delay = BASE_GENOME['universe'], '1'

    try:
        from . import datafield_palette as _pal
        pal_rows = _pal._all_rows()
    except Exception:
        pal_rows = []

    hid = None
    n = 0
    skipped_mixed = []
    for field in cands[:n_axes]:
        if field in AXES:
            partners = (NEUT_FIELD, 'rsk70_mfm2_gemtrd_earnqlty')  # 검증된 원본 골격
        else:
            partners = _same_dataset_partners(field, pal_rows)
            if partners is None:
                # 같은 데이터셋에 짝이 없으면 **다른 데이터셋에서 빌린다**
                # (2026-08-18 사장 지시). 원래는 여기서 통째로 건너뛰었는데,
                # 그 규칙이 제출 가능한 조합을 시작도 못 하게 막고 있었다 —
                # 08-17~18 에 실제로 통과한 알파 3건은 전부 **데이터셋이 다른 둘**의
                # 합성이었다(EMEA 가 되는 재료와 IS_LADDER 가 되는 재료가 서로 다른
                # 데이터셋에만 있다). 한 재료로 다섯 조건을 다 만족시킬 수 없다.
                # 대가는 피라미드 귀속이 흐려지는 것인데, 칸을 여는 것보다 제출이
                # 먼저다(칸은 분기 내내 남는다).
                partners = _cross_dataset_partners(field, pal_rows)
                if partners is None:
                    skipped_mixed.append(field)
                    continue
        for sign in (1, -1):
            g = dict(BASE_GENOME, universe=universe, sign=sign,
                     fields=[field, partners[0], partners[1]])
            try:
                code = genome_models.render(genome_models._coerce_genome(g))
                if alpha_lint.validate_alpha(code):
                    continue
            except Exception as e:
                LOG.warning('푸시 스펙 렌더 실패 %s sign=%s: %s', field, sign, e)
                continue
            if hid is None:
                hid = _db.insert_hypothesis(
                    _db.latest_run_id(user_id), user_id,
                    {'title': f'{TITLE} {_time.strftime("%Y-%m-%d", _time.localtime(now))}',
                     'rationale': '검증 골격(vector_neut vs indmom, CROWDING) × 신선 축 — '
                                  '소진(제출됨)·사망(S<1) 축 제외 자동 로테이션.'})
            _db.insert_spec(hid, user_id, genome=g, code=code,
                            settings={'universe': universe, 'neutralization': g['neutralization'],
                                      'decay': str(g['decay']), 'truncation': '0.08',
                                      'nan_handling': 'OFF', 'delay': delay},
                            delay=delay, why=f'{field.rsplit("_", 1)[-1]} 축 sign={sign:+d}')
            n += 1
    if skipped_mixed and log_fn:
        log_fn('⏭ 같은 데이터셋 짝을 못 찾아 건너뜀(혼합 코드는 피라미드 칸이 어긋난다): '
               + ', '.join(skipped_mixed))
    if n and log_fn:
        axes = ', '.join(f.rsplit('_', 1)[-1] for f in cands[:n_axes])
        tail = f' · 미달 칸 [{", ".join(sorted(short))}]' if short else ''
        log_fn(f'{"🎯 자동 제출 푸시" if focused else "🌱 상시 다변화"} — 오늘 제출 '
               f'{done}/{DAILY_SUBMIT_TARGET}, 신선 축 [{axes}] ±양부호 스펙 {n}개 장전{tail}')
    return n
