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

import logging
import os
import time as _time

from . import db as _db
from .pace_keeper import DAY_BOUNDARY_HOUR

LOG = logging.getLogger('genomicwqb.submit_push')

DAILY_SUBMIT_TARGET = int(os.environ.get('IQC_PUSH_DAILY_SUBMITS', '1'))
# 집중 창 — 경계(13:00) 직전 3시간(=10:00부터) 동안만, 웨이브를 라운드마다 연사한다
# (2026-08-02 사장: "기준 오전 10시로 해서 3시간동안 빡세게"). 쿨다운은 겹장전
# 방지용 바닥값일 뿐, 실제 페이싱은 라운드 주기(15~25분)가 맡는다.
PUSH_WINDOW_S = float(os.environ.get('IQC_PUSH_WINDOW_S', str(3 * 3600)))
COOLDOWN_S = float(os.environ.get('IQC_PUSH_COOLDOWN_S', '600'))
DEAD_AXIS_SHARPE = 1.0        # 이 골격 시도의 최고 S 가 이 미만이면 죽은 축
AXES_PER_WAVE = 4             # 축 4 × 부호 2 = 스펙 8 (라운드 limit)
TITLE = '자동 제출 푸시'

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


def _discover_axes(exclude: set, n: int) -> list[str]:
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
    try:
        from . import run_config
        c = run_config.get_constraint()
        region = (str(getattr(c, 'region', '') or '') or 'GLB').upper()
        universe = (str(getattr(c, 'universe', '') or '') or 'TOPDIV3000').upper()
        delay = str(getattr(c, 'delay', '') or '1')
    except Exception:
        region, universe, delay = 'GLB', 'TOPDIV3000', '1'
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
        cands.append((used, -cov, name))   # 저사용 → 고커버리지(GLB 강건성) 순
    cands.sort()
    return [name for _, __, name in cands[:n]]


def _axis_stats(user_id: int) -> tuple[set, dict]:
    """(소진 축, 축별 최고 S) — 최근 30일 코드 실측.
    '시도'는 이 골격(indmom 대비 잔차)에서 신호 쪽에 쓰인 경우만 센다."""
    burned, best = set(), {}
    for code, sharpe, submitted in _db.code_sharpe_submitted_since(
            user_id, _time.time() - 30 * 86400):
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
    return burned, best


def maybe_seed(user_id: int, log_fn=None, now: float | None = None) -> int:
    """라운드 시작 훅 — 집중 창(10:00~13:00) 동안 오늘 제출이 목표 미달이면
    신선 축 스펙을 라운드마다 장전한다. 큐레이션 축이 마르면 카탈로그에서 발굴.
    반환: 주입한 스펙 수. 어떤 실패도 라운드를 막으면 안 된다(호출측 try/except)."""
    now = _time.time() if now is None else now
    if not _in_window(now):
        return 0
    if _db.submitted_count_since(user_id, _day0(now)) >= DAILY_SUBMIT_TARGET:
        return 0
    if _db.pending_specs(user_id, limit=1):
        return 0                                  # 탄약 소화 중 — 겹장전 금지
    last = _db.last_hypothesis_ts(user_id, TITLE)
    if last and now - last < COOLDOWN_S:
        return 0
    burned, best = _axis_stats(user_id)
    cands = [f for f in AXES if f not in burned and best.get(f, 9.9) >= DEAD_AXIS_SHARPE]
    if len(cands) < AXES_PER_WAVE:
        tried = burned | set(best) | set(AXES)
        found = _discover_axes(tried, AXES_PER_WAVE - len(cands))
        if found and log_fn:
            log_fn(f'🔭 축 발굴 — 카탈로그에서 신규 {len(found)}개: '
                   + ', '.join(found))
        cands += found
    if not cands:
        if log_fn:
            log_fn('🚨 자동 제출 푸시 — 큐레이션·카탈로그 모두 축이 고갈됐습니다 '
                   '(발굴 필터를 점검해 주세요).')
        return 0

    from . import alpha_lint, genome_models
    try:
        from . import run_config
        spec_c = run_config.get_constraint()
        universe = str(getattr(spec_c, 'universe', '') or '') or BASE_GENOME['universe']
        delay = str(getattr(spec_c, 'delay', '') or '1')
    except Exception:
        universe, delay = BASE_GENOME['universe'], '1'

    hid = None
    n = 0
    for field in cands[:AXES_PER_WAVE]:
        for sign in (1, -1):
            g = dict(BASE_GENOME, universe=universe, sign=sign,
                     fields=[field, NEUT_FIELD, 'rsk70_mfm2_gemtrd_earnqlty'])
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
    if n and log_fn:
        axes = ', '.join(f.rsplit('_', 1)[-1] for f in cands[:AXES_PER_WAVE])
        log_fn(f'🎯 자동 제출 푸시 — 오늘 제출 0/{DAILY_SUBMIT_TARGET}, '
               f'신선 축 [{axes}] ±양부호 스펙 {n}개 장전')
    return n
