"""selection — HYFE_IQC 씨앗/부모 선택용 다목적(multi-objective) 유틸 (pure, stdlib only).

#6: rank-백분위 정규화(rank_percentile) + NSGA-II 비지배 정렬(non_dominated_sort) +
    crowding distance. 고정 REF 가중합(reward.py)의 분포-비적응 한계를 보완한다.
#5: '세대 내 다양성'은 NSGA-II crowding(목적공간 밀집도)으로 자연 통합되고, percentile
    모드에는 구조적(코드 Jaccard) fitness-sharing 을 선택적으로 얹는다(sim_fn 주입).

계약:
- 모든 목적벡터는 **최대화** 기준(낮을수록 좋은 turnover/self_corr 는 부호 반전해 넣는다).
- 순수: IO/전역상태 없음, 예외 안 던짐(방어). 작은 씨앗풀 대상이라 O(n^2) 로 충분.

reward.py 와의 관계: reward.compute_reward 는 여전히 per-alpha 스칼라(밴딧/시딩 rank 하위호환).
여기(selection)는 population-level 로 그 위에 백분위/파레토 '선택층'만 얹는다 — 즉시 롤백 가능.
"""
from __future__ import annotations


def rank_percentile(values, invert=False):
    """각 값을 population 내 백분위 [0,1] 로. 최고=1.0. None → 최악(0). invert=True 면
    낮을수록 좋음(turnover/self_corr). n<=1 은 중립 0.5."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]

    def better(a, b):
        if a is None:
            return False
        if b is None:
            return True
        return (a < b) if invert else (a > b)

    out = []
    for i, v in enumerate(values):
        worse = sum(1 for j, u in enumerate(values) if j != i and better(v, u))
        out.append(worse / (n - 1))
    return out


def _dominates(a, b) -> bool:
    """a 가 b 를 지배(모든 목적 >= 이고 하나는 >). 목적은 전부 최대화 기준."""
    ge = all(x >= y for x, y in zip(a, b))
    gt = any(x > y for x, y in zip(a, b))
    return ge and gt


def non_dominated_sort(objs):
    """objs[i]=개체 i 의 목적벡터(최대화). 파레토 프론트들의 리스트(front0=최상)."""
    n = len(objs)
    if n == 0:
        return []
    dominated = [[] for _ in range(n)]   # p 가 지배하는 q 들
    ndom = [0] * n                        # p 를 지배하는 개체 수
    fronts = [[]]
    for p in range(n):
        for q in range(n):
            if p == q:
                continue
            if _dominates(objs[p], objs[q]):
                dominated[p].append(q)
            elif _dominates(objs[q], objs[p]):
                ndom[p] += 1
        if ndom[p] == 0:
            fronts[0].append(p)
    i = 0
    while i < len(fronts) and fronts[i]:
        nxt = []
        for p in fronts[i]:
            for q in dominated[p]:
                ndom[q] -= 1
                if ndom[q] == 0:
                    nxt.append(q)
        i += 1
        fronts.append(nxt)
    return [f for f in fronts if f]


def crowding_distance(objs):
    """한 프론트(또는 임의 집합)의 개체별 crowding distance. 경계 개체는 inf(다양성 보존).
    입력 순서와 동일 순서로 반환."""
    n = len(objs)
    if n == 0:
        return []
    if n <= 2:
        return [float('inf')] * n
    m = len(objs[0])
    dist = [0.0] * n
    for k in range(m):
        order = sorted(range(n), key=lambda i: objs[i][k])
        lo = objs[order[0]][k]
        hi = objs[order[-1]][k]
        rng = hi - lo
        if rng == 0:
            # ⚠ 축퇴(모두 같은 값) 축에는 경계가 **없다**. 그런데도 inf 를 찍으면
            #   '임의의 두 개체'가 무한 우선순위를 얻는다 — 지표가 평평한 구간에서
            #   라운드 1의 화석이 영구 엘리트가 되는 경로였다(2026-07-14 실측).
            #   inf 부여를 rng 검사 **뒤로** 옮겨 이 축은 아무 기여도 하지 않게 한다.
            continue
        dist[order[0]] = float('inf')
        dist[order[-1]] = float('inf')
        for r in range(1, n - 1):
            dist[order[r]] += (objs[order[r + 1]][k] - objs[order[r - 1]][k]) / rng
    return dist


def nsga2_order(objs, tiebreak=None):
    """NSGA-II 순서: (프론트 오름차, 프론트 내 crowding 내림차) 로 best→worst 인덱스.

    `tiebreak[i]` — crowding 이 같을 때 **큰 쪽이 이긴다**. 여기에 알파 id 를 넣는 것이
    anti-fossil 불변식을 지키는 열쇠다: 지표가 평평하면 프론트 전원의 crowding 이
    동률(경계는 inf)이라, 타이브레이크가 없으면 **라운드 1의 화석이 영구 엘리트**가 된다
    (2026-07-14, nsga2 를 기본으로 켜자마자 재현 — round-66 동결 사건의 재발).
    """
    order = []
    for front in non_dominated_sort(objs):
        fobjs = [objs[i] for i in front]
        cd = crowding_distance(fobjs)
        ranked = sorted(
            range(len(front)),
            key=lambda r: (cd[r], (tiebreak[front[r]] if tiebreak is not None else 0)),
            reverse=True)
        order.extend(front[r] for r in ranked)
    return order


# ── HYFE 씨앗 레코드 어댑터 ────────────────────────────────────────────────

def _f(x):
    """지표 → float|None. '9.45%' 같은 단위 접미사를 반드시 풀어야 한다 — 브라우저 시대
    알파는 퍼센트 문자열로 저장돼 있어서, 순진한 float() 는 ValueError → None 이 되고
    turnover/returns 가 '미측정' 으로 둔갑한다(reward._fopt docstring 참조)."""
    from .reward import _fopt
    return _fopt(x)


# turnover 목적의 '충분히 낮음' 바닥 (2026-07-21 이전 기준. 아래 obj_vector 주석 참조).
TURNOVER_FLOOR = 0.125


def measured_2y(d):
    """레코드의 실측 2Y Sharpe. 없으면 None (레거시 행·브라우저 시대)."""
    return _f((d.get('metrics') or {}).get('sharpe_2y'))


def obj_vector(d, sharpe_2y_default=None) -> list:
    """씨앗 레코드 → 목적벡터 (전부 최대화):
        [sharpe, fitness, route(제출 가능성), -self_corr]  (+ sharpe_2y 는 아래 참조)

    - route: **2026-07-21 방향 전환.** 3번째 축은 원래 `-turnover_excess`(회전율이
      낮을수록 좋음)였는데, 규칙 개편으로 그 축이 정확히 반대가 됐다 — 고회전(HTVR)
      분류(회전율>20%)를 얻어야 표준 컷이 WARNING 으로 강등돼 제출이 열린다.
      그래서 '제출까지의 거리'(criteria.submittability = HT·표준 경로 중 가까운 쪽)로
      바꾼다. 축 개수는 그대로라 파레토/crowding 코드는 손대지 않는다.
    - sharpe_2y: `sharpe_2y_default` 가 None 이면 이 축을 **아예 넣지 않는다**.
      호출부(order_seed_records)가 population 전체를 보고 결정한다 — 아무도 측정값이
      없으면 축 자체가 무의미하고, 결측을 sharpe 로 대체하면 **sharpe 가 두 축에
      이중 계산돼 파레토 지배가 왜곡된다**(2026-07-14 실측: turnover·self_corr 가 나쁜
      고-Sharpe 알파가 그 덕에 승격). 측정값이 있는 population 에서 결측 행은
      호출부가 넘긴 중립값(측정값들의 중앙값)을 받는다 — 가점도 감점도 아니다.
    결측: sharpe/fitness→0, turnover→0.5(약한 페널티), self_corr→0(중립, 미측정 흔함).
    """
    m = d.get('metrics') or {}
    sharpe = d.get('_sharpe')
    if sharpe is None:
        sharpe = _f(m.get('sharpe'))
    sharpe = sharpe if sharpe is not None else 0.0
    fitness = _f(m.get('fitness'))
    turnover = _f(m.get('turnover'))
    self_corr = _f(d.get('self_corr'))
    if self_corr is None:
        self_corr = _f(m.get('self_corr'))
    from . import criteria as _criteria
    from . import reward as _reward
    # ⚠ 2026-07-22: 회전율이 차단 컷(70%)을 넘은 알파의 Sharpe/Fitness 는 **명목상
    #   숫자**다 — 그 회전율로는 제출 자체가 안 된다(HIGH_TURNOVER FAIL). 그런데 감쇄
    #   없이 넣으면 회전율 143% · Sharpe 1.11 짜리가 **sharpe 축 최고**라 아무에게도
    #   지배당하지 않아 파레토 1층에 영구히 남는다. 실제로 보상(selection_score)을
    #   고친 뒤에도 시드 풀 상위가 그대로였던 원인이 이것이다(r623 실측: 자식 8개 중
    #   5개가 회전율 70% 초과, 중앙 103.8%). 선택 경로가 둘(점수/목적벡터)이라
    #   한쪽만 고치면 다른 쪽이 폭주 부모를 계속 살린다.
    _damp = _reward.damp_if_positive
    # 결측 turnover 는 '미측정' 이지 '완벽' 이 아니다 — route 항이 0 으로 떨어진다.
    _t = turnover if turnover is not None else 0.0
    out = [
        _damp(sharpe, _t),
        _damp(fitness if fitness is not None else 0.0, _t),
        _criteria.submittability(m) if turnover is not None else 0.0,
        -(self_corr if self_corr is not None else 0.0),
    ]
    try:
        from . import run_config as _run_config
        _v2 = _run_config.is_architecture_v2_enabled()
    except Exception:
        _v2 = False
    if _v2:
        from . import research_v2 as _v2_policy
        dims = _v2_policy.portfolio_dimensions(m)
        # 지역 값이 없으면 0, PROD 가 아직 관측되지 않았으면 컷(0.7)을 중립값으로 둔다.
        # 실측 0.62는 -0.62로 미관측 -0.70보다 우위, 0.83은 열위가 된다.
        out.extend([
            _damp(dims['min_region_sharpe'] or 0.0, _t),
            -(dims['prod_correlation']
              if dims['prod_correlation'] is not None else 0.7),
        ])
    if sharpe_2y_default is not None:
        v = measured_2y(d)
        out.append(_damp(v if v is not None else float(sharpe_2y_default), _t))
    return out


def _obj_matrix(records) -> tuple:
    """(목적행렬, 2y축 포함여부). 2y 는 population 에 측정값이 하나라도 있을 때만 넣는다."""
    measured = [v for v in (measured_2y(d) for d in records) if v is not None]
    if not measured:
        return [obj_vector(d) for d in records], False
    s = sorted(measured)
    neutral = s[len(s) // 2]              # 중앙값 = 결측 행의 중립값
    return [obj_vector(d, sharpe_2y_default=neutral) for d in records], True


def composite_scores(records, weights=None):
    """rank-백분위 가중합 스코어(레코드 순서). 각 목적을 population 백분위로 정규화 후 가중합.
    목적 순서 = obj_vector: [sharpe, fitness, -turnover, -self_corr] (+ sharpe_2y 가 있으면 뒤에)."""
    if not records:
        return []
    objs, has_2y = _obj_matrix(records)
    try:
        from . import run_config as _run_config
        _v2 = _run_config.is_architecture_v2_enabled()
    except Exception:
        _v2 = False
    if weights:
        w = list(weights)
    elif _v2 and has_2y:
        w = [0.22, 0.16, 0.12, 0.08, 0.17, 0.10, 0.15]
    elif _v2:
        w = [0.27, 0.19, 0.14, 0.09, 0.19, 0.12]
    elif has_2y:
        w = [0.32, 0.24, 0.12, 0.12, 0.20]
    else:
        w = [0.4, 0.3, 0.2, 0.1]
    cols = list(zip(*objs))               # 목적별 컬럼
    pct = [rank_percentile(list(c), invert=False) for c in cols]   # 이미 최대화 부호
    n_obj = min(len(w), len(pct))
    return [sum(w[k] * pct[k][i] for k in range(n_obj)) for i in range(len(records))]


def _greedy_diversified(records, scores, lam, sim_fn):
    """percentile 스코어에 구조적 fitness-sharing: 매번 (score - lam·최대유사도(선택된 것)) 최대 선택.
    sim_fn(codeA, codeB)->[0,1]. lam<=0 또는 sim_fn 없으면 순수 score 내림차순."""
    n = len(records)
    codes = [(d.get('code') or '') for d in records]
    remaining = set(range(n))
    order = []
    while remaining:
        best_i, best_val = None, None
        for i in remaining:
            pen = 0.0
            if order and lam > 0 and sim_fn is not None:
                try:
                    pen = lam * max(sim_fn(codes[i], codes[j]) for j in order)
                except Exception:
                    pen = 0.0
            val = scores[i] - pen
            if best_val is None or val > best_val:
                best_val, best_i = val, i
        order.append(best_i)
        remaining.discard(best_i)
    return order


def order_seed_records(records, *, mode='ref', weights=None, lam=0.0, sim_fn=None):
    """씨앗 레코드 정렬 인덱스(best→worst). mode 'ref' → None(호출부가 기존 정렬 사용).
      - 'nsga2': 비지배정렬 + crowding(목적공간 다양성 = #5). 동률은 **최신 우선**.
      - 'percentile': rank-백분위 가중합. lam>0 & sim_fn 이면 구조적 다양성(#5) fitness-sharing.
    """
    if not records:
        return list(range(len(records)))
    objs, _has_2y = _obj_matrix(records)
    if mode == 'nsga2':
        # crowding 동률(지표가 평평한 구간)에서 옛 행이 눌러앉지 않도록 id 로 타이브레이크.
        ids = [_f(d.get('id')) or 0.0 for d in records]
        return nsga2_order(objs, tiebreak=ids)
    if mode == 'percentile':
        scores = composite_scores(records, weights)
        if lam > 0 and sim_fn is not None:
            return _greedy_diversified(records, scores, lam, sim_fn)
        return sorted(range(len(records)), key=lambda i: scores[i], reverse=True)
    return None
