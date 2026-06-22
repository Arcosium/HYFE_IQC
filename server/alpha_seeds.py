"""alpha_seeds — low-turnover WQB FASTEXPR seed templates (WorldQuant 101 Alphas).

A palette the generator samples from and is told to VARY (hypothesis/window/field),
NOT alphas to emit verbatim. Every expr here is already concrete and MUST parse +
lint clean (see tests/test_alpha_seeds.py contract test). Invariants:
  - all time windows are integers
  - group_neutralize uses BARE group names (sector/industry/subindustry/market)
  - hump uses the named arg: hump(x, hump=0.03)
  - no scientific notation (0.000001, not 1e-6)
"""
from __future__ import annotations

import random as _random

SEED_TEMPLATES: list[dict] = [
    {"family": "pv_corr_reversion",
     "expr": "-1 * ts_corr(rank(close), rank(volume), 10)",
     "ops": ["ts_corr", "rank"],
     "intuition": "음의 가격-거래량 상관 = 평균회귀 (이중 rank으로 이상치 제거)"},
    {"family": "decayed_ranked_corr",
     "expr": "-1 * ts_rank(ts_decay_linear(ts_corr(group_neutralize(vwap, sector), volume, 4), 8), 6)",
     "ops": ["ts_rank", "ts_decay_linear", "ts_corr", "group_neutralize"],
     "intuition": "섹터중립 vwap/volume 상관을 decay+ts_rank로 3중 평활 → 최저 회전"},
    {"family": "short_revert_long_corr",
     "expr": "scale(ts_mean(close, 7) - close) + 20 * scale(ts_corr(vwap, ts_delay(close, 5), 230))",
     "ops": ["scale", "ts_mean", "ts_corr", "ts_delay"],
     "intuition": "7일 단기 반전 + 230일 장기 vwap/지연close 상관 (장기창=저회전)"},
    {"family": "scale_combo",
     "expr": "scale(ts_corr(adv20, low, 5) + (high + low) / 2 - close)",
     "ops": ["scale", "ts_corr"],
     "intuition": "중간가+adv/low상관 vs close 괴리, scale로 달러중립"},
    {"family": "stochastic_reversal",
     "expr": "-1 * ts_corr(rank((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12) + 0.000001)), rank(volume), 6)",
     "ops": ["ts_corr", "rank", "ts_min", "ts_max"],
     "intuition": "범위내위치(%R)와 거래량 rank의 음상관 = 반전"},
    {"family": "vwap_relative_ratio",
     "expr": "rank(vwap - close) / rank(vwap + close)",
     "ops": ["rank"],
     "intuition": "vwap 대비 저평가 틸트 — stateless, 추가 회전 거의 0"},
    {"family": "decayed_fitness_vwapclose",
     "expr": "ts_decay_linear(rank((vwap - close) / (close + 0.000001)), 5)",
     "ops": ["ts_decay_linear", "rank"],
     "intuition": "측정된 고-Fitness(~2.86) vwap-close 반전 decay"},
    {"family": "avdiff_corr_gate",
     "expr": "-1 * ts_av_diff(close, 50) * ts_corr(close, volume, 50)",
     "ops": ["ts_av_diff", "ts_corr"],
     "intuition": "구조적으로 유효할 때만 진입하는 상관 게이트 (측정 Fitness~1.70)"},
    {"family": "double_rank_momentum",
     "expr": "rank(ts_rank(close / ts_delay(close, 5) - 1, 40))",
     "ops": ["rank", "ts_rank", "ts_delay"],
     "intuition": "시계열+횡단면 이중 rank 모멘텀 → 안정적, 경계화"},
    {"family": "min_blend_bounded",
     "expr": "min(rank(ts_decay_linear((rank(open) + rank(low)) - (rank(high) + rank(close)), 8)), ts_rank(ts_decay_linear(ts_corr(ts_rank(close, 8), ts_rank(adv60, 21), 8), 7), 3))",
     "ops": ["min", "rank", "ts_decay_linear", "ts_rank", "ts_corr"],
     "intuition": "모든 항이 경계화/decay → 매끄럽고 가중치 분산 양호"},
    # ── 필드위생 래퍼 winsorize(ts_backfill(F,120),std=4) + 검증된 스켈레톤 (고-Sharpe 표준) ──
    {"family": "hygiene_fundamental_skeleton",
     "expr": "-1 * rank(ts_decay_linear(ts_corr(group_neutralize(winsorize(ts_backfill(operating_income, 120), std=4), sector), winsorize(ts_backfill(assets, 120), std=4), 5), 8))",
     "ops": ["rank", "ts_decay_linear", "ts_corr", "group_neutralize", "winsorize", "ts_backfill"],
     "intuition": "위생래퍼+섹터중립 펀더멘털 스켈레톤 — Sharpe 0.2 차단의 표준 골격"},
    {"family": "hygiene_two_factor_value",
     "expr": "rank(winsorize(ts_backfill(cashflow_op, 120), std=4) / (winsorize(ts_backfill(cap, 120), std=4) + 0.000001)) * rank(ts_delta(close, 20))",
     "ops": ["rank", "winsorize", "ts_backfill", "ts_delta"],
     "intuition": "2팩터(현금흐름수익률×가격모멘텀) 각각 rank 후 결합 — 단일팩터 천장 탈출"},
    {"family": "hygiene_analyst_zscore",
     "expr": "-1 * ts_zscore(winsorize(ts_backfill(anl4_bvps_mean, 120), std=4), 63)",
     "ops": ["ts_zscore", "winsorize", "ts_backfill"],
     "intuition": "위생래퍼 애널리스트 BVPS 의 63일 zscore 역전 (jglazar 류 Sharpe~2.0)"},
]

FAMILIES: list[str] = sorted({t["family"] for t in SEED_TEMPLATES})


def sample_seeds(n, families=None, exclude_ops=None, rng=None) -> list[dict]:
    """Return up to n seed dicts.

    families:    keep only these family tags (None = all).
    exclude_ops: drop templates using any excluded operator (SC-saturation lever).
    rng:         random.Random for deterministic sampling (None = module default).
    Never raises.
    """
    try:
        rng = rng or _random.Random()
        exclude_ops = set(exclude_ops or ())
        fam = set(families) if families is not None else None
        pool = [t for t in SEED_TEMPLATES
                if (fam is None or t["family"] in fam)
                and not (set(t["ops"]) & exclude_ops)]
        rng.shuffle(pool)
        return pool[:max(0, int(n))]
    except Exception:
        return []


def render_seeds_section(seeds) -> str:
    """Render seeds as a prompt section; empty string when no seeds."""
    if not seeds:
        return ''
    lines = ['[검증된 저회전 시드 — 통째 베끼지 말고 가설/창/필드를 변형해 사용 (WorldQuant 101 기반)]']
    for s in seeds:
        lines.append(f'- ({s.get("family","")}) {s.get("expr","")}  // {s.get("intuition","")}')
    return '\n'.join(lines)
