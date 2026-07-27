"""superalpha — OS 알파 풀 위의 슈퍼알파 리서치 경로 (⑤, WQB AAF SuperAlpha 이식).

AAF 레시피 그대로: 필터(롱숏비·turnover·datafield 수·OS 시작일) → 의사난수
선택(turnover/long_count 를 시드로 쓰는 ACE 트릭 — 특정 위험으로의 쏠림 없이
복원추출) → combo 그리드(alpha_stats × {rank, ts_ir, ts_rank} × 부호) →
type=SUPER 시뮬. **자동 제출 없음** — 결과는 superalpha_runs 에 기록만 하고
제출 판단은 사람이 한다.

게이트: IQC_SUPERALPHA=1 + RC 계정일 때만 /api/superalpha/run 이 동작한다.
표현식 생성부는 순수 함수 (run() 만 네트워크/DB).

ponytail: 선택 셋 자체의 PnL 상관검사(원본 노트북의 max_corr<0.5 드롭)는 뺐다 —
알파당 PnL GET 이 필요해 쿼터가 아깝다. 슈퍼알파 시뮬 결과가 나쁘면 그게 곧
같은 신호다. 필요해지면 get_alpha_pnl 상관 게이트를 여기 run() 에 추가할 것.
"""
from __future__ import annotations

import logging
import threading

from . import criteria as _criteria
from . import db as _db

LOG = logging.getLogger('genomicwqb.superalpha')

SELECTION_LIMIT = 10
SELECTION_HANDLING = 'NON_NAN'

# combo 그리드 축 (AAF: 이 셋 밖 연산자는 효율이 나빴다고 명시).
COMBO_OPS = ('rank', 'ts_ir', 'ts_rank')
COMBO_STATS = ('hold_value', 'short_count/stats.long_count', 'pnl', 'returns',
               'turnover')


def selection_expression(seed_plus: int, *,
                         datafield_count_max: int = 8,
                         longshort_bal: float = 0.1,
                         os_start_date: str = '2023-01-01') -> str:
    """필터 조건 × 의사난수 정렬키 (ACE 노트북 형식 그대로).

    sqrt(turnover+d)*10000 의 소수부는 d 만 바뀌어도 사실상 난수 — 알파 스탯을
    시드로 쓰는 복원추출이라 특정 위험 노출로 쏠리지 않는다(AAF Sort-alphas).
    """
    turnover_max = _criteria.SUPERALPHA_TURNOVER_MAX
    conditions = '*'.join([
        f'(datafield_count < {int(datafield_count_max)})',
        f'(abs(short_count/long_count-1) < {longshort_bal})',
        f'(turnover < {turnover_max})',
        f'(turnover > {_criteria.SUPERALPHA_TURNOVER_MIN})',
        f'(os_start_date > "{os_start_date}")',
    ])
    return (f'd = {int(seed_plus)};\n'
            f'{conditions}*\n'
            f'(sqrt(turnover+d)*10000-round(sqrt(turnover+d)*10000)+\n'
            f'sqrt(long_count+d)*10000-round(sqrt(long_count+d)*10000))')


def combo_grid() -> list[str]:
    """alpha_stats × 연산자 (+ -drawdown 축) — AAF Combine-alphas 그리드."""
    out = []
    for stat in COMBO_STATS:
        for op in COMBO_OPS:
            win = ',126' if op.startswith('ts') else ''
            out.append(f'stats = generate_stats(alpha);\n{op}(stats.{stat}{win})')
    for op in COMBO_OPS:
        win = ',126' if op.startswith('ts') else ''
        out.append(f'stats = generate_stats(alpha);\n{op}(-stats.drawdown{win})')
    return out


def default_settings() -> dict:
    """SUPER 시뮬 settings 완성본 (ACE generate_super_alpha 기본값)."""
    return {
        'nanHandling': 'OFF', 'instrumentType': 'EQUITY', 'delay': 1,
        'universe': 'TOP3000', 'truncation': 0.08, 'unitHandling': 'VERIFY',
        'pasteurization': 'ON', 'region': 'USA', 'language': 'FASTEXPR',
        'decay': 0, 'neutralization': 'INDUSTRY', 'visualization': False,
        'selectionHandling': SELECTION_HANDLING,
        'selectionLimit': SELECTION_LIMIT,
    }


def build_candidates(seed_plus: int, *, n: int = 6, rng=None) -> list[dict]:
    """selection 1개 × combo 그리드에서 n 개 표본 → [{selection, combo, settings}]."""
    sel = selection_expression(seed_plus)
    combos = combo_grid()
    if rng is not None and n < len(combos):
        combos = rng.sample(combos, n)
    else:
        combos = combos[:n]
    st = default_settings()
    return [{'selection': sel, 'combo': c, 'settings': dict(st)} for c in combos]


def run(user_id: int, username: str, password: str, *,
        seed_plus: int = 10, n: int = 6, rng=None,
        stop_event=None) -> int | None:
    """슈퍼알파 리서치 1런 — 백그라운드 스레드에서 호출된다. → run_id.

    후보를 순차 시뮬한다(ACE 권장 동시 2 이지만 일일 쿼터 공유라 순차가 안전).
    결과/실패는 superalpha_runs 에 영속화. 예외는 error 로 기록하고 삼킨다.
    """
    cands = build_candidates(seed_plus, n=n, rng=rng)
    run_id = _db.superalpha_start(user_id, seed_plus, cands[0]['selection'])
    results: list[dict] = []
    try:
        from . import wqb_api as _wqb_api
        client = _wqb_api.WqbApiClient(username, password)
        if not client.authenticate():
            _db.superalpha_finish(run_id, 'error', results, 'WQB 인증 실패')
            return run_id
        for c in cands:
            if stop_event is not None and stop_event.is_set():
                break
            loc = client.submit_super_simulation(c['selection'], c['combo'],
                                                 c['settings'])
            if loc == 'NOT_PERMISSIONED':
                # 계정 권한 문제 — 남은 후보를 돌려봐야 전부 같은 400 이다. 즉시 끝낸다.
                _db.superalpha_finish(
                    run_id, 'error', results,
                    'WQB 계정에 super simulation 권한이 없습니다. CONSULTANT 권한만으로는 '
                    '안 되고 별도 권한이 필요합니다 — WorldQuant 에 요청하세요.')
                return run_id
            if loc in (None, 'RATE_LIMITED'):
                results.append({'combo': c['combo'], 'error': str(loc or 'submit 실패')})
                continue
            done = client.poll(loc, stop_event=stop_event)
            alpha_id = done.get('alpha')
            entry: dict = {'combo': c['combo'], 'alpha_id': alpha_id,
                           'status': done.get('status')}
            if alpha_id:
                try:
                    h = client.harvest_alpha(alpha_id) or {}
                    entry['metrics'] = h.get('metrics') or {}
                    entry['is_status'] = h.get('is_status') or {}
                except Exception as e:
                    entry['error'] = f'harvest: {e}'
            else:
                entry['error'] = str(done.get('message') or '')[:200]
            results.append(entry)
        _db.superalpha_finish(run_id, 'done', results)
    except Exception as e:
        LOG.exception('superalpha run 실패')
        _db.superalpha_finish(run_id, 'error', results, str(e))
    return run_id


def start_background(user_id: int, username: str, password: str, *,
                     seed_plus: int = 10, n: int = 6) -> None:
    t = threading.Thread(
        target=run, args=(user_id, username, password),
        kwargs={'seed_plus': seed_plus, 'n': n},
        daemon=True, name=f'superalpha-{user_id}')
    t.start()
