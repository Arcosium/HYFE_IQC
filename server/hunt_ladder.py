"""hunt_ladder — '차단된 알파를 제출권으로 밀어 올리는' 표준 처방 사다리.

2026-07-27 GLB 사냥에서 사람이 손으로 돌린 판단 과정을 그대로 규칙화한 것이다.
그날 40여 회 시뮬로 얻은 실측 교훈이 전부 여기 들어 있다:

 1. **부호**: |Sharpe| 가 크고 음수면 실패가 아니라 방향이 반대다.
    (CLV 지표: ts_rank → S=-0.98 → 부호 반전 → +1.01)
 2. **회전율은 창이 아니라 사후 감쇠로 잡는다**.
    창을 늘리면 신호가 죽는다(5일 1.01 → 10일 0.36 → 20일 0.46).
    신호 창은 그대로 두고 ts_decay_linear 로 **포지션 변화만** 평활하면
    회전율이 반토막 나면서 Sharpe 는 오히려 오른다(1.18→0.60, S 1.01→1.07).
    → 이게 그날의 결승타였다.
 3. **Fitness 는 회전율의 함수다**. F = S×√(|ret|/max(TO,0.125)) 이므로
    회전율만 내려도 Fitness 가 함께 오른다(0.14→0.21). 별도 처방이 필요 없다.
 4. **수준 ≠ 변화**: 학술 이상현상(MAX 등)은 수준(rank)이 신호다.
    ts_zscore 로 감싸면 '변화'를 재게 되어 효과가 사라진다(0.36 → 0.06).
 5. **RAM 중립화**: 고회전 테마에서 HT_ORTHOGONAL_RAM_NEUTRALIZATION 이
    WARNING 이면 REVERSION_AND_MOMENTUM 중립화로 바꿔 PASS 를 노린다.

순수 모듈(IO/DB 없음, 예외 안 던짐). 후보는 워커의 기존 파이프라인을 그대로 탄다.
"""
from __future__ import annotations

import re

from .reward import _f

IDX_BASE = 61          # 유전체 1..8 / 재조합 31+ / 개선 41+ / HT구제 51+ 와 겹치지 않게

# 이 사다리를 태울 최소 신호 세기. 이보다 약하면 처방해도 문턱에 못 간다.
MIN_ABS_SHARPE = 0.8
# 제출 적격 회전율 상한 (Power Pool). 이걸 넘으면 감쇠 처방 대상.
TURNOVER_CAP = 0.70
# 사후 감쇠 길이 사다리 — 20 이 그날의 승자, 앞뒤를 함께 훑는다.
DECAY_LINEAR_LADDER = (20, 30, 12)
RAM_NEUTRALIZATION = 'REVERSION_AND_MOMENTUM'


def _wrap_decay_linear(code: str, n: int) -> str:
    return f'ts_decay_linear({code}, {int(n)})'


def _flip(code: str) -> str:
    """부호 반전. 이미 -1* 로 시작하면 벗겨 낸다(이중 반전 방지)."""
    c = code.strip()
    m = re.match(r'^-\s*1\s*\*\s*\((.*)\)$', c, re.S)
    if m:
        return m.group(1)
    if c.startswith('-') and not c.startswith('--'):
        return c[1:].strip()
    return f'-1 * ({c})'


def diagnose(metrics: dict, blocking_names=None) -> dict:
    """알파의 상태를 사다리 관점으로 진단한다 (순수)."""
    m = metrics or {}
    s = _f(m.get('sharpe'))
    to = _f(m.get('turnover'))
    fit = _f(m.get('fitness'))
    names = {str(n or '').upper() for n in (blocking_names or [])}
    return {
        'sharpe': s, 'turnover': to, 'fitness': fit,
        'sign_wrong': s <= -MIN_ABS_SHARPE,
        'strong_enough': abs(s) >= MIN_ABS_SHARPE,
        'turnover_over': to > TURNOVER_CAP,
        'fitness_short': 0 < fit < 1.0,
        # 회전율·샤프·핏만 막혔으면 처방 가능. 구조적 실패(에러·상관 등)는 대상 아님.
        'remediable': bool(names) and names <= {
            'LOW_SHARPE', 'LOW_FITNESS', 'HIGH_TURNOVER', 'LOW_TURNOVER'},
    }


def remedies(code: str, metrics: dict, settings: dict | None = None,
             blocking_names=None, *, n: int = 4, ht_ram_warning: bool = False) -> list[dict]:
    """차단된 알파 → 처방 후보 ≤ n 개. 못 고칠 상태면 [].

    각 dict 은 워커 strategies 항목과 같은 모양:
    {idx, code, desc, settings, origin='hunt'}
    """
    try:
        code = str(code or '').strip()
        if not code or n <= 0:
            return []
        d = diagnose(metrics, blocking_names)
        if not d['strong_enough']:
            return []

        base = _flip(code) if d['sign_wrong'] else code
        st = dict(settings or {})
        out: list[tuple[str, str, dict]] = []

        if d['sign_wrong']:
            # 1순위: 부호만 뒤집어 그대로 재측정 (가장 싼 처방)
            out.append((base, f'🧭 부호 반전 (S={d["sharpe"]:.2f} → 방향 반대)', dict(st)))

        # 2순위: 회전율/Fitness 처방 = **사후 감쇠**(창은 건드리지 않는다)
        if d['turnover_over'] or d['fitness_short']:
            for k in DECAY_LINEAR_LADDER:
                out.append((_wrap_decay_linear(base, k),
                            f'⚙ 사후 감쇠 decay_linear({k}) — 회전율 {d["turnover"]:.2f} 절감',
                            dict(st)))
            out.append((f'hump({base}, hump=0.03)',
                        '⚙ hump 0.03 — 문턱 미만 변화 무시로 회전 억제', dict(st)))

        # 3순위: RAM 중립화 (고회전 테마의 직교성 체크 PASS 노림)
        if ht_ram_warning and str(st.get('neutralization', '')).upper() != RAM_NEUTRALIZATION:
            st_ram = dict(st)
            st_ram['neutralization'] = RAM_NEUTRALIZATION
            src = _wrap_decay_linear(base, DECAY_LINEAR_LADDER[0]) if d['turnover_over'] else base
            out.append((src, '🧲 RAM 중립화 — HT 직교성 체크 PASS 노림', st_ram))

        seen: set[tuple] = set()
        picked = []
        for c, desc, s in out:
            key = (c, str(sorted(s.items())))
            if key in seen or c == code:
                continue
            seen.add(key)
            picked.append({'idx': IDX_BASE + len(picked), 'code': c, 'desc': desc,
                           'settings': s, 'origin': 'hunt'})
            if len(picked) >= n:
                break
        return picked
    except Exception:
        return []
