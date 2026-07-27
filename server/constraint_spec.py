"""constraint_spec — **탐색 조건**을 기계가 읽는 명세로 바꾼다.

무엇을 푸는가
-------------
"이 조건에 맞는 알파를 찾아라" 는 요구가 상시로 들어온다. Power Pool 주간 테마가
대표적이고(매주 필터가 바뀐다), 대회·캠페인·즉석 지시도 같은 모양이다. 예:

    region=USA & delay=1 & universe=TOP1000 &
    High Turnover returns ratio test PASS & datasets not in ['pv1']

    "USA 딜레이1 TOP1000에서 pv1 제외하고 고회전 수익보존 통과하는 알파"

이걸 매번 사람이 읽고 손으로 헌팅하면 재현이 안 된다. 조건을 **타입드 객체**로 만들어
GA 생성기(genome_models)에 먹이면, 워커가 도는 내내 조건 밖 알파를 아예 만들지 않는다.

핵심 설계
---------
- 순수 데이터·순수 함수다. IO 도, 시뮬도, 전역상태도 없다.
- `parse()` 는 필터 문법과 자연어(한/영)를 **둘 다** 받는다.
- 못 읽은 절은 **버리지 않고 `unparsed` 에 남긴다.** 조용히 무시하면 조건을 놓친 채
  "충족" 이라 착각한다 — 제출은 하루 4건뿐이라 한 건이 비싸다.
- `compliant()` 는 bool 이 아니라 (ok, reasons) 를 준다. 왜 떨어졌는지가 곧 다음 수다.
- **모르는 정보는 불충족으로 본다.** 확인 못 한 걸 통과로 세면 예산을 버린다.

출처: BRAIN "Power Pool Alphas" · "Overview of Themes" · "Theme Calendar"
(docs/brain_learn/) + 2026-07-21~22 라이브 발굴 실측.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 자연어/캘린더 표기 → WQB 설정값.
# 캘린더는 소문자 자연어("slow and fast"), API 는 대문자 스네이크를 쓴다.
NEUTRALIZATION_ALIAS = {
    'slow and fast': 'SLOW_AND_FAST',
    'slow_and_fast': 'SLOW_AND_FAST',
    'reversion and momentum': 'REVERSION_AND_MOMENTUM',
    'reversion_and_momentum': 'REVERSION_AND_MOMENTUM',
    'statistical': 'STATISTICAL',
    'subindustry': 'SUBINDUSTRY',
    'crowding': 'CROWDING',
    'industry': 'INDUSTRY',
    'market': 'MARKET',
    'sector': 'SECTOR',
    'slow': 'SLOW',
    'fast': 'FAST',
    'ram': 'REVERSION_AND_MOMENTUM',
    'none': 'NONE',
}

# "…test PASS" 로 요구되는 항목 → 실제 IS check 이름.
CHECK_ALIAS = {
    'high turnover returns ratio': 'HT_HIGH_TURNOVER_RETURNS_RATIO',
    'high turnover returns': 'HT_HIGH_TURNOVER_RETURNS_RATIO',
    '고회전 수익보존': 'HT_HIGH_TURNOVER_RETURNS_RATIO',
    '수익보존': 'HT_HIGH_TURNOVER_RETURNS_RATIO',
    'pnl realization horizon': 'HT_PNL_REALIZATION_HORIZON',
    'pnl 실현지평': 'HT_PNL_REALIZATION_HORIZON',
    '실현지평': 'HT_PNL_REALIZATION_HORIZON',
    'after cost sharpe': 'HT_AFTER_COST_SHARPE',
    '후비용': 'HT_AFTER_COST_SHARPE',
    'sub universe': 'LOW_SUB_UNIVERSE_SHARPE',
    'cluster': 'CLUSTER_TEST',
}

KNOWN_REGIONS = ('USA', 'GLB', 'CHN', 'EUR', 'ASI', 'JPN', 'KOR', 'TWN',
                 'HKG', 'AMR', 'IND', 'MEA')
# 비-컨설턴트가 뛰는 판(IQC)은 USA 고정이다. delay 는 0/1 중 선택 가능하고,
# 리전만 못 고른다 (2026-07-27 사장 확인). ConstraintSpec.for_account 가 강제한다.
IQC_REGION = 'USA'
# 일반 계정이 실제로 쓸 수 있는 중립화 (2026-07-27 실계정 실측).
# 나머지(STATISTICAL·CROWDING·FAST·SLOW·SLOW_AND_FAST·REVERSION_AND_MOMENTUM)는
# "Neutralization X is not available." 로 400 이 난다 — 컨설턴트 전용이다.
# ⚠ 이걸 안 걸면 GLB Power Pool 테마의 중립화 목록이 그대로 넘어와 시뮬이 전멸한다.
IQC_NEUTRALIZATIONS = ('NONE', 'MARKET', 'INDUSTRY', 'SUBINDUSTRY', 'SECTOR')


@dataclass
class ConstraintSpec:
    """탐색을 가둘 조건. 비어 있는 필드는 '제약 없음' 이다."""
    raw: str = ''
    label: str = ''
    region: str | None = None
    delay: str | None = None
    universe: str | None = None
    neutralizations: tuple = ()          # 빈 튜플이면 제약 없음
    excluded_datasets: frozenset = frozenset()
    required_checks: tuple = ()
    unparsed: tuple = ()

    def is_empty(self) -> bool:
        return not (self.region or self.delay or self.universe or self.neutralizations
                    or self.excluded_datasets or self.required_checks)

    def for_account(self, account_type: str) -> 'ConstraintSpec':
        """계정 종류가 강제하는 규칙을 덧씌운 조건.

        비-컨설턴트는 **IQC(International Quant Championship) 규칙**을 따른다:
        리전은 무조건 USA, delay 는 0/1 중 선택 가능. 전역 조건이 GLB 같은 다른
        리전을 가리켜도 일반 계정은 그 리전으로 경쟁할 수 없으므로 여기서 가둔다
        (2026-07-27 사장 지시). RC 는 조건을 그대로 쓴다.
        """
        import dataclasses
        if account_type == 'research_consultant':
            return self
        # 중립화는 리전과 무관하게 계정 등급에 매인다 — 조건이 컨설턴트 전용 중립화만
        # 지정했다면 남는 게 없으므로 제약을 아예 푼다(유전체가 기본 5종에서 고른다).
        allowed = tuple(n for n in self.neutralizations if n in IQC_NEUTRALIZATIONS)
        same_region = self.region == IQC_REGION
        if same_region and allowed == self.neutralizations:
            return self
        return dataclasses.replace(
            self,
            region=IQC_REGION,
            neutralizations=allowed,
            # 유니버스는 리전에 매인다 — 다른 리전 것을 들고 가면 시뮬이 죽는다.
            universe=(self.universe if self.region in (None, IQC_REGION) else None),
            label=(f'{self.label} · IQC 규칙(USA·기본 중립화)' if self.label
                   else 'IQC 규칙(USA·기본 중립화)'))

    def settings_base(self) -> dict:
        """이 조건이 **강제**하는 시뮬 설정 조각. 나머지(decay·truncation 등)는 자유다."""
        out = {}
        if self.region:
            out['region'] = self.region
        if self.delay is not None:
            out['delay'] = int(self.delay)
        if self.universe:
            out['universe'] = self.universe
        return out

    def allows_neutralization(self, neut) -> bool:
        if not self.neutralizations:
            return True
        return str(neut or '').strip().upper() in self.neutralizations

    def allows_dataset(self, dataset_id) -> bool:
        return str(dataset_id or '').strip().lower() not in self.excluded_datasets

    def describe(self) -> str:
        """사람이 읽는 한 줄 요약 (워커 로그·대시보드용)."""
        bits = []
        if self.region:
            bits.append(self.region)
        if self.delay is not None:
            bits.append(f'D{self.delay}')
        if self.universe:
            bits.append(self.universe)
        if self.neutralizations:
            bits.append('중립화 ' + '/'.join(self.neutralizations))
        if self.excluded_datasets:
            bits.append('제외 ' + ','.join(sorted(self.excluded_datasets)))
        if self.required_checks:
            bits.append('요구 ' + ','.join(self.required_checks))
        return ' · '.join(bits) if bits else '(제약 없음)'

    def compliant(self, *, settings=None, datasets=None, checks=None) -> tuple:
        """이 알파가 조건을 통과하는가 → (ok, 사유목록).

        settings — 시뮬 설정 dict (region/delay/universe/neutralization)
        datasets — 알파가 쓴 데이터셋 id 목록 (None 이면 '미확인' 으로 불충족)
        checks   — {체크이름: 결과문자열}
        """
        s = {str(k): v for k, v in (settings or {}).items()}
        reasons = []

        def _up(v):
            return str(v).strip().upper() if v is not None else None

        if self.region and _up(s.get('region')) != self.region.upper():
            reasons.append(f"region={s.get('region')} (요구 {self.region})")
        if self.delay is not None and str(s.get('delay')) != str(self.delay):
            reasons.append(f"delay={s.get('delay')} (요구 {self.delay})")
        if self.universe and _up(s.get('universe')) != self.universe.upper():
            reasons.append(f"universe={s.get('universe')} (요구 {self.universe})")
        if self.neutralizations and not self.allows_neutralization(s.get('neutralization')):
            reasons.append(f"neutralization={s.get('neutralization')} "
                           f"(허용 {'/'.join(self.neutralizations)})")
        if self.excluded_datasets:
            if datasets is None:
                reasons.append('데이터셋 미확인')
            else:
                bad = sorted({str(d).strip().lower() for d in datasets} & self.excluded_datasets)
                if bad:
                    reasons.append(f"금지 데이터셋 사용: {', '.join(bad)}")
        ck = {str(k).upper(): str(v).upper() for k, v in (checks or {}).items()}
        for need in self.required_checks:
            got = ck.get(need)
            if got != 'PASS':
                reasons.append(f"{need}={got or '미측정'} (요구 PASS)")
        return (not reasons), reasons


def _parse_list(blob: str) -> list:
    """['pv1', 'model110'] 또는 (slow, fast, ram) 안의 항목을 뽑는다."""
    inner = blob.strip().strip('[]()')
    return [x.strip().strip('\'"') for x in inner.split(',') if x.strip().strip('\'"')]


def _split_clauses(raw: str) -> list:
    """`&` 와 (괄호 밖의) ` and ` 로 자른다. 괄호 안 쉼표는 건드리지 않는다."""
    parts, buf, depth = [], '', 0
    for tok in re.split(r'(\s*&\s*|\s+and\s+|\s+AND\s+)', raw):
        if re.fullmatch(r'\s*&\s*|\s+and\s+|\s+AND\s+', tok or '') and depth == 0:
            parts.append(buf)
            buf = ''
            continue
        depth += (tok or '').count('(') + (tok or '').count('[')
        depth -= (tok or '').count(')') + (tok or '').count(']')
        buf += tok or ''
    if buf.strip():
        parts.append(buf)
    return parts


def _harvest_natural(text: str, spec: ConstraintSpec, neuts: list,
                     excluded: set, checks: list) -> None:
    """구조화 파싱이 못 잡은 것을 자연어에서 줍는다 (한/영 공통).

    구조화 절이 이미 채운 필드는 **덮어쓰지 않는다** — 명시적 문법이 항상 우선이다.
    """
    low = text.lower()

    if spec.region is None:
        for r in KNOWN_REGIONS:
            if re.search(rf'(?<![a-z]){r.lower()}(?![a-z])', low):
                spec.region = r
                break
    if spec.delay is None:
        m = (re.search(r'(?:delay|딜레이|지연)\s*[=:]?\s*([01])(?![0-9])', low)
             or re.search(r'(?<![a-z0-9])d\s*([01])(?![0-9])', low))
        if m:
            spec.delay = m.group(1)
    if spec.universe is None:
        m = re.search(r'(?<![a-z])(top[a-z0-9]+)', low)
        if m:
            spec.universe = m.group(1).upper()
    if not neuts:
        # 긴 별칭부터 봐야 "slow and fast" 가 "slow"/"fast" 로 쪼개지지 않는다.
        for key in sorted(NEUTRALIZATION_ALIAS, key=len, reverse=True):
            if re.search(rf'(?<![a-z_]){re.escape(key)}(?![a-z_])', low):
                val = NEUTRALIZATION_ALIAS[key]
                if val not in neuts:
                    neuts.append(val)
    # "pv1 제외/금지/빼고/without/except/no pv1"
    for m in re.finditer(r'([a-z][a-z0-9_]*)\s*(?:은|는|을|를)?\s*'
                         r'(?:제외|금지|빼고|except|without|excluding)', low):
        excluded.add(m.group(1))
    for m in re.finditer(r'(?:제외|금지|without|excluding|no)\s+([a-z][a-z0-9_]*)', low):
        excluded.add(m.group(1))
    for key, val in CHECK_ALIAS.items():
        if key in low and val not in checks:
            # 자연어에선 'PASS' 를 명시 안 해도 요구로 본다("고회전 수익보존 통과하는").
            checks.append(val)


def _clause_understood(clause: str, spec: ConstraintSpec, neuts: list,
                       excluded: set, checks: list) -> bool:
    """이 절의 내용이 결국 어딘가에 반영됐는가 (자연어 보충 이후 판정)."""
    low = clause.lower()
    if spec.region and spec.region.lower() in low:
        return True
    if spec.universe and spec.universe.lower() in low:
        return True
    if any(d in low for d in excluded):
        return True
    if any(k in low for k, v in NEUTRALIZATION_ALIAS.items() if v in neuts):
        return True
    if any(k in low for k, v in CHECK_ALIAS.items() if v in checks):
        return True
    return False


def parse(text, label: str = '') -> ConstraintSpec:
    """조건 문자열(필터 문법 또는 자연어) → ConstraintSpec.

    필터 문법 예:
      region=USA & delay=1 & universe=TOP1000 &
        neutralization in (slow, fast, ram, statistical, crowding) &
        datasets not in ['pv1']

    자연어 예:
      "USA 딜레이1 TOP1000에서 pv1 제외하고 고회전 수익보존 통과하는 알파"
    """
    raw = (text or '').strip()
    spec = ConstraintSpec(raw=raw, label=label)
    if not raw:
        return spec

    neuts, excluded, checks, unparsed = [], set(), [], []
    for p in _split_clauses(raw):
        c = p.strip().rstrip(',').strip()
        if not c:
            continue
        m = re.match(r'region\s*[=:]\s*([A-Za-z]+)', c, re.I)
        if m:
            spec.region = m.group(1).upper()
            continue
        m = re.match(r'delay\s*[=:]\s*([01])', c, re.I)
        if m:
            spec.delay = m.group(1)
            continue
        m = re.match(r'universe\s*[=:]\s*([A-Za-z0-9_]+)', c, re.I)
        if m:
            spec.universe = m.group(1).upper()
            continue
        m = re.search(r'neutralizations?\s+in\s*(\(.*?\)|\[.*?\])', c, re.S | re.I)
        if m:
            for item in _parse_list(m.group(1)):
                key = item.strip().lower()
                neuts.append(NEUTRALIZATION_ALIAS.get(key, key.upper().replace(' ', '_')))
            continue
        m = re.search(r'datasets?\s+not\s+in\s*(\(.*?\)|\[.*?\])', c, re.S | re.I)
        if m:
            excluded.update(x.strip().lower() for x in _parse_list(m.group(1)))
            continue
        m = (re.match(r'(.+?)\s+test\s+PASS\s*$', c, re.I)
             or re.match(r'(.+?)\s+PASS\s*$', c, re.I))
        if m:
            name = m.group(1).strip().lower()
            checks.append(CHECK_ALIAS.get(name, name.upper().replace(' ', '_')))
            continue
        unparsed.append(c)

    # 구조화로 못 채운 부분을 자연어에서 보충한다.
    _harvest_natural(raw, spec, neuts, excluded, checks)
    # 자연어로 건진 절은 unparsed 에서 뺀다 (이해했으니 경고할 이유가 없다).
    still = [u for u in unparsed
             if not _clause_understood(u, spec, neuts, excluded, checks)]

    spec.neutralizations = tuple(dict.fromkeys(neuts))
    spec.excluded_datasets = frozenset(excluded)
    spec.required_checks = tuple(dict.fromkeys(checks))
    spec.unparsed = tuple(still)
    return spec


# ── 문법 예시 ────────────────────────────────────────────────────────────────
# 2026-07 Power Pool 캘린더에서 실제로 걸렸던 조건들. **하드코딩된 일정이 아니라
# 문법 예시**다 — 조건은 run_config 로 언제든 갈아끼운다.
EXAMPLES = {
    'usa_d1_top1000_neut':
        "region=USA & delay=1 & universe=TOP1000 & neutralization in "
        "(slow, fast, slow and fast, ram, statistical, crowding) & datasets not in ['pv1']",
    'usa_d1_top1000_htvr':
        "region=USA & delay=1 & universe=TOP1000 & "
        "High Turnover returns ratio test PASS & datasets not in ['pv1']",
    'glb_d1_topdiv3000':
        "region=GLB & delay=1 & universe=TOPDIV3000 and neutralization in "
        "(slow, fast, slow and fast, ram, crowding) and datasets not in ['pv1', 'model110']",
}
