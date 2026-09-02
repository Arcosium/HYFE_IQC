"""정향변이(directed mutation)의 온라인 학습 — '어떤 조정이 어떤 지표를 고치더라'.

부모→자식 귀속 엣지(alphas.parent_alpha_id + alphas.directive)에서
(부모 FAIL 사유 category, 적용한 변이 축 directive) → 성공률 행렬을 집계하고,
다음 변이 축을 Thompson sampling 으로 고른다. 기존 규칙(genome_models._directives)은
사전확률(prior)로 남아 cold-start 에서도 규칙 기반과 동등 이상으로 동작한다.

순수 모듈 — IO/DB import 없음. 무작위성은 호출자가 넘긴 random.Random 만 사용
(GA 의 라운드 결정론을 깨지 않기 위해). DB 집계는 db.directive_stats() 가 담당.

주의(키 정합): 선택 시점의 fail_items 는 focus 큐가 실어 온 desc 문자열
('Turnover(0.85>0.7)')이고, 집계 시점의 fail_items 는 DB 에 저장된 name 문자열
('Turnover')이다. name 에는 방향(</>)이 없어 turnover_low 관측은 사실상 쌓이지
않는데, 그 경우 choose_directive 가 사전확률(=규칙 'sharpen')로 자연 폴백하므로
동작은 안전하다.
"""
from __future__ import annotations

import random
from typing import Iterable

# 변이 축 전집합 — genome_models._mutate 의 분기와 1:1.
# 2026-07-14 신설: 'boost'(Fitness 병목=returns 를 올린다) · 'robustify'(2Y Sharpe).
# 2026-07-21 신설: 'churn' — 회전율을 **올려** 고회전(HTVR) 분류 문턱(20%)을 넘긴다.
#   제출 규칙 개편으로 이 축이 사실상 제출 가능성의 주 레버가 됐다(criteria.py 참조).
# ⚠ 새 축은 **끝에 붙인다**. choose_directive 가 이 순서대로 rng.betavariate 를 뽑으므로,
#   중간에 끼워 넣으면 기존 축들의 난수 위치가 밀려 학습된 선택이 통째로 달라진다.
DIRECTIVES = ('smooth', 'sharpen', 'concentration', 'universe', 'decorrelate',
              'signal', 'boost', 'robustify', 'churn', 'region_balance')

# FAIL 사유 category → 규칙 기반 기본 변이 축 (기존 _directives 규칙의 단일 진실).
# 학습 데이터가 없을 때 Thompson 사전확률을 이 매핑이 결정한다.
RULE_DIRECTIVE = {
    'turnover_high': 'smooth',
    # 2026-07-21: 회전율 과소는 이제 단순한 '신호 민감도' 문제가 아니라 **제출 자격**
    # 문제다 — 20% 를 못 넘으면 고회전 분류를 못 얻고, 그러면 D0 Sharpe 2.69 를
    # 정면으로 뚫어야 한다. 전용 축 'churn' 으로 보낸다.
    'turnover_low': 'churn',
    # HT 관문(회전율/수익보존/지평) 미달 — fail_items 엔 안 나오고 metrics 로만 보인다.
    'ht_gap': 'churn',
    'sub_universe': 'universe',
    'correlation': 'decorrelate',
    'concentration': 'concentration',
    # LOW_FITNESS 는 여태 'signal'(무작위 변이) 로 뭉뚱그려졌다. 그런데 라이브 병목은
    # 정확히 Fitness 다 — Fitness = Sharpe·sqrt(|Returns|/max(Turnover,0.125)) 이고
    # turnover 는 이미 바닥(~3%)이라 **returns 를 올리는 것만이 유일한 레버**다.
    'fitness': 'boost',
    # LOW_2Y_SHARPE / IS_LADDER_SHARPE = '최근 2년에도, 구간을 잘라도 통하는가'.
    # 전 구간 Sharpe 만 좋고 최근이 무너진 알파를 겨냥한 별도 축.
    'stability': 'robustify',
    # GLB 전체는 좋아도 한 지역만 무너지면 제출할 수 없다. 신호식을 갈아엎지 않고
    # 중립화·그룹·절단 축만 움직여 지역 최저점을 끌어올린다.
    'regional': 'region_balance',
    'signal': 'signal',
}

# Thompson 사전확률 (Beta(a,b) 의사 관측). 규칙이 가리키는 축은 '성공 3/실패 1'
# 상당으로 시작해 초기엔 규칙대로 가고, 관측이 쌓이면 데이터가 사전확률을 이긴다.
PRIOR_RULE = (3.0, 1.0)
PRIOR_OTHER = (1.0, 3.0)


def categorize(fail_items: Iterable[str] | None, metrics: dict | None = None) -> list[str]:
    """fail 사유 문자열들 → canonical category 리스트 (중복 유지, 순서 보존).

    genome_models._directives 의 파싱 규칙과 동일해야 한다(그쪽이 이 함수를 쓴다).

    metrics 를 주면 **fail_items 에 안 나타나는 병목**도 잡는다: 고회전(HTVR) 관문
    미달은 FAIL 이 아니라 WARNING 이라 사유 문자열에 없는데, 개편된 규칙에선 그게
    제출을 막는 진짜 원인이다(회전율 20% 미만 → 표준 컷 강등을 못 받음 → LOW_SHARPE
    FAIL). 이 경우 'ht_gap' 을 **맨 앞에** 넣어 churn 축이 우선 선택되게 한다.
    """
    out: list[str] = []
    if metrics:
        try:
            from . import criteria as _criteria
            st = _criteria.ht_status(metrics)
            if not st['eligible'] and 'turnover' in (st.get('gaps') or []):
                out.append('ht_gap')
        except Exception:
            pass
    for it in fail_items or []:
        s = str(it).lower()
        # 순서 중요: 'LOW_SUB_UNIVERSE_SHARPE' 는 sharpe 이전에 sub-universe 로 잡아야 한다.
        if 'sub' in s and ('universe' in s or 'sharpe' in s):
            out.append('sub_universe')
        elif 'correlation' in s or 'self-corr' in s or 'self corr' in s:
            out.append('correlation')
        elif 'turnover' in s:
            # '<'(desc 표기)·'below'(WQB 원문)·low_turnover 는 과소, 그 외는 과다.
            out.append('turnover_low'
                       if ('<' in s or 'low_turnover' in s or 'below' in s)
                       else 'turnover_high')
        elif 'weight' in s or 'concentr' in s:
            out.append('concentration')
        # 'LOW_2Y_SHARPE' · 'IS_LADDER_SHARPE' — 둘 다 이름에 'sharpe' 가 있어 아래
        # 일반 signal 분기에 삼켜지고 있었다. 시간 안정성은 신호 세기와 다른 문제다.
        elif '2y' in s or 'ladder' in s:
            out.append('stability')
        elif 'fitness' in s:
            out.append('fitness')
        elif ('glb' in s and any(k in s for k in ('amer', 'emea', 'apac'))
              and 'sharpe' in s):
            out.append('regional')
        elif any(k in s for k in ('sharpe', 'returns', 'margin', 'drawdown')):
            out.append('signal')
    return out


def choose_directive(fail_items: Iterable[str] | None,
                     stats: dict | None,
                     rng: random.Random,
                     metrics: dict | None = None) -> str | None:
    """부모의 fail 사유 + 누적 관측으로 변이 축 1개를 Thompson sampling 으로 선택.

    stats: db.directive_stats() 산출 — {(category, directive): {'n':int,'wins':int}}.
           None/빈 dict 이어도 동작한다(사전확률만으로 표집 = 규칙 우세 + 소량 탐색).
    반환: DIRECTIVES 중 하나, 또는 fail 사유가 하나도 분류되지 않으면 None.
    """
    cats = categorize(fail_items, metrics)
    if not cats:
        return None
    uniq_cats = list(dict.fromkeys(cats))
    rule_dirs = {RULE_DIRECTIVE[c] for c in uniq_cats}
    best: str | None = None
    best_theta = -1.0
    for d in DIRECTIVES:
        a0, b0 = PRIOR_RULE if d in rule_dirs else PRIOR_OTHER
        wins = 0.0
        losses = 0.0
        for c in uniq_cats:
            st = (stats or {}).get((c, d))
            if st:
                w = float(st.get('wins') or 0)
                wins += w
                losses += max(0.0, float(st.get('n') or 0) - w)
        theta = rng.betavariate(a0 + wins, b0 + losses)
        if theta > best_theta:
            best, best_theta = d, theta
    return best


# category → (metrics 키, 클수록 좋은가). 표적 지표가 실제로 나아졌는지 재는 데 쓴다.
_TARGET_METRIC = {
    'signal': ('sharpe', True),
    'fitness': ('fitness', True),
    'stability': ('sharpe_2y', True),
    'sub_universe': ('sub_universe_sharpe', True),
    'correlation': ('self_correlation', False),
    'concentration': ('weight_concentration', False),
    'turnover_high': ('turnover', False),
    'turnover_low': ('turnover', True),
    # 고회전 관문 — 회전율이 올라갔으면 전진으로 인정한다(20% 를 넘겨야 분류를 얻는다).
    'ht_gap': ('turnover', True),
    'regional': ('min_region_sharpe', True),
}
# 표적 지표가 없을 때의 대리 지표 (2Y·sub-universe 는 브라우저 시대 행에 없다).
_FALLBACK_METRIC = {'sharpe_2y': 'sharpe', 'sub_universe_sharpe': 'sharpe'}

# 개선으로 인정하는 최소 상대 변화. 노이즈를 승리로 세지 않기 위한 문턱.
IMPROVE_EPS = 0.02


def _metric(metrics: dict, key: str):
    """metrics[key] → float|None. '9.45%' 같은 단위 접미사도 푼다(브라우저 시대 포맷)."""
    v = (metrics or {}).get(key)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    unit = 1.0
    if s.endswith('%'):
        unit, s = 1.0 / 100, s[:-1]
    elif s.endswith('‱'):
        unit, s = 1.0 / 10000, s[:-1]
    try:
        return float(s.replace(',', '').strip()) * unit
    except ValueError:
        return None


def _improved(cat: str, parent: dict, child: dict) -> bool | None:
    """그 category 의 표적 지표가 부모 대비 나아졌는가. 잴 수 없으면 None."""
    spec = _TARGET_METRIC.get(cat)
    if not spec:
        return None
    key, higher_is_better = spec
    pm, cm = parent.get('metrics') or {}, child.get('metrics') or {}
    if key == 'min_region_sharpe':
        region_keys = ('glb_amer_sharpe', 'glb_emea_sharpe', 'glb_apac_sharpe')
        p_values = [_metric(pm, k) for k in region_keys]
        c_values = [_metric(cm, k) for k in region_keys]
        if any(v is None for v in p_values + c_values):
            return None
        pv, cv = min(p_values), min(c_values)
    else:
        pv, cv = _metric(pm, key), _metric(cm, key)
    if pv is None or cv is None:
        alt = _FALLBACK_METRIC.get(key)
        if not alt:
            return None
        pv, cv = _metric(pm, alt), _metric(cm, alt)
        if pv is None or cv is None:
            return None
    margin = IMPROVE_EPS * max(abs(pv), 1e-9)
    return (cv > pv + margin) if higher_is_better else (cv < pv - margin)


def outcome_observations(parent: dict, child: dict) -> list[tuple[str, str, bool]]:
    """부모→자식 엣지 1개 → (category, directive, win) 관측 리스트.

    win 의 정의 — 그 directive 가 '겨냥한 문제를 실제로 개선했는가':
      (a) 해당 category 가 자식의 fail 사유에서 **사라졌거나** (표적 해소), 또는
      (b) 그 category 의 **표적 지표가 유의미하게 나아졌거나** (부분 전진)
      AND 자식 pass_count 가 부모보다 후퇴하지 않았고 (다른 걸 부수지 않음)
      AND 자식이 시뮬 에러가 아니다.

    ⚠ (b) 가 없으면 학습이 원리적으로 죽는다. 라이브 실측(2026-07-14): LOW_SHARPE 와
      LOW_FITNESS 가 알파의 ~100% 에서 FAIL 이라 (a) 만으로는 **모든 축이 영원히 0승**
      이다 — 실제 directive_stats 가 전 축 0/219, 0/120 … 이었다. 그러면 Thompson
      사후분포가 관측된 축을 전부 짓눌러, 시도해 본 적 없는 축만 뽑히는 상태로 굳는다.
      Sharpe 0.3 → 0.9 처럼 '아직 통과는 못 했지만 확실히 전진' 한 변이를 실패로 세면
      배울 수 있는 게 남지 않는다.

    부모의 category 마다 관측 1개씩 낸다 — directive 하나가 여러 fail 을 동시에
    겨냥할 수 있으므로 category 별로 따로 채점해야 행렬이 된다.
    """
    d = str(child.get('directive') or '').strip()
    if not d:
        return []
    p_cats = list(dict.fromkeys(categorize(parent.get('fail_items') or [])))
    if not p_cats:
        return []
    c_cats = set(categorize(child.get('fail_items') or []))
    regressed = int(child.get('pass_count') or 0) < int(parent.get('pass_count') or 0)
    errored = bool(str(child.get('error_text') or '').strip())
    out: list[tuple[str, str, bool]] = []
    for c in p_cats:
        resolved = c not in c_cats
        improved = _improved(c, parent, child) is True
        out.append((c, d, (resolved or improved) and not regressed and not errored))
    return out
