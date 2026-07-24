# HYFE_IQC 개선 설계안 + 구현 완료 — 2026-07-08

4개 오픈소스(WQ-Brain·WorldQuantBrain-Agent·brain_viewer·YongHui-X/Quant) 정독 후,
사장님이 채택한 7개 방향의 설계안 + **구현 완료(2026-07-08)**. 테스트 623 pass(신규 +40).

## ✅ 구현 완료 상태 & 배포 요건

| # | 상태 | 주 파일 | 반영 방법 | 기본값 |
|---|---|---|---|---|
| 1 | ✅ | `wqb_api.py` | **서비스 재시작 필요** | 항상 on |
| 7 | ✅ | `static/{app.js,index.html,style.css}` (v=8) | **정적서빙 — 새로고침만** | 항상 표시 |
| 3 | ✅ | `wqb_data_service.py`·`operator_catalog.py` | 재시작 + **house RC 계정 새로고침**으로 `data/live_operators.csv` 생성돼야 arity 활성 | on(데이터 없으면 inert) |
| 2 | ✅ | `presim_gate.py`·`alpha_ast.py`·`datafield_palette.py` | 재시작 | field=on(팔레트 有 시)·arity=on(시그니처 有 시) |
| 4 | ✅ | `alpha_repair.py`·`worker.py` | 재시작 | on |
| 6 | ✅ | `selection.py`(신규)·`db.py` | 재시작 + **`IQC_SELECTION_MODE` 설정 시 활성** | **off('ref'=기존 동작)** |
| 5 | ✅ | `selection.py`·`db.py` | #6 nsga2 crowding + `IQC_SELECTION_DIVERSITY_LAM` | off |

**환경변수 킬스위치/전환** (전부 재시작 반영):
- `IQC_REAUTH_THRESHOLD_S`(기본 900) — #1 선제갱신 임계.
- `IQC_PRESIM_FIELD_CHECK=0` / `IQC_PRESIM_ARITY_CHECK=0` — #2 개별 차단.
- `IQC_GUIDED_REPAIR=0` — #4 차단.
- `IQC_SELECTION_MODE=ref|percentile|nsga2`(기본 ref) — #6 선택층 전환. **P2는 #7 관측 위에서 롤아웃**.
- `IQC_SELECTION_DIVERSITY_LAM`(기본 0, 예 0.3) — #5 구조적 다양성 강도(percentile 모드).

**주의(P2 롤아웃)**: #6/#5 는 기본 off. 켜기 전 #7 Observatory(파레토/리더보드)로 before/after 관측.
#2 field-check 는 정적 팔레트만으로도 활성 — 첫 재시작 후 라이브피드의 `⊘ 사전게이트 드롭` 을
모니터. 전량 드롭 시 워커가 자동으로 전량 통과(fail-safe)하므로 throughput 0 이 되진 않는다.

---

## (이하 원본 설계안)

## 핵심 발견 — 이건 "신규 추가"가 아니라 "델타/정련" 계획

정독한 4개 레포는 대부분 HYFE보다 **덜** 성숙하다. HYFE는 이미:

- `reward.py` — self-corr novelty 패널티(>0.7→0, 0.3~0.7 선형) + 가중 정규화 composite + all-pass/overfit 게이트 **완비**
- `presim_gate.py` — 시뮬 전 구조적 사전게이트(복잡도·구조적 near-dup overlap)
- `alpha_repair.py` — 시뮬 전 결정적 패턴 자동수리(typo·filter attr·region prefix·doubled op·hump·quoted group·missing lookback)
- `alpha_lint.py` + `alpha_ast.py` — AST 검증(균형괄호·vector-datafield 오용)
- `operator_catalog.py`(brain_operators.csv) + `datafield_palette.py`(정적 + `data/live_datafields.csv`)
- `wqb_data_service.py` — 하우스 RC 계정으로 region×universe×delay 그리드별 `/data-fields` 실시간 수집
- `settings_sweep.py` — region-legal universe×neutralization 스윕(`_sanitize_settings`가 불법 조합 접힘)
- `alpha_similarity.py` — operator/field Jaccard, `alpha_search.py` — 세대 diversity(CV)

따라서 각 방향은 **현재 상태 대비 정확한 델타**만 서술한다.

## 요약 표

| # | 워크스트림 | 현재 상태 | 델타(할 일) | 주 구현 지점 | 우선순위 |
|---|---|---|---|---|---|
| 1 | OPTIONS 헬스체크+선제갱신 | `_session_valid`가 `GET /authentication` | `OPTIONS /simulations`로 교체 + `token.expiry` 임박 시에만 갱신 | `wqb_api.py` | **P0** |
| 2 | 시뮬 전 로컬 사전검증 | AST가 미지 식별자 통과, arity 미검증 | 라이브 region 팔레트 대비 **field 존재** + **operator arity/params** 하드 프리플라이트 | `alpha_lint.py`·`alpha_ast.py`·`presim_gate.py` | P1 |
| 3 | 카탈로그=유전자 + region 제약 | datafields 라이브 수집 有, `/operators`는 정적 CSV | 라이브 `/operators`(arity/params) 수집 + 변이가 **genome의 region 팔레트만** 사용 보장 | `wqb_data_service.py`·`operator_catalog.py`·`alpha_mutate.py` | P1 |
| 4 | 가이드 리페어 연산자 | 사전 패턴 수리(피드백 無) | 시뮬 **에러 메시지** 파싱→표적 1회 수리(최근접 field 스냅 등)→재큐 | `alpha_repair.py`·`worker.py` | P1 |
| 5 | 세대 내 다양성 항 | 제출풀 self-corr penalty 完, presim overlap 有 | **부모선택에 crowding/sharing** 항(세대 내 Jaccard 거리) | `reward.py`(또는 신규 `selection.py`)·`worker.py` | P2 |
| 6 | rank-백분위 composite + 파레토 | 고정 REF 가중합 스칼라 | 라운드 분포 기반 **rank-백분위 정규화** + **NSGA-II 비지배 정렬** 선택층 | `reward.py`·`worker.py` | P2 |
| 7 | Observatory 시각화 업그레이드 | 무의존 SVG, 세대별 단일지표 막대 | 기준선·품질색상·하이퍼파라미터 평균·산점도·파레토뷰·리더보드 | `static/app.js`·`static/index.html` | **P0** |

---

## 1. OPTIONS 헬스체크 + `token.expiry` 선제갱신  (P0)

**현재.** `wqb_api.py:162 _session_valid()`가 `GET {BASE}/authentication`으로 세션 유효성을 판정.
`authenticate()`는 (1) 저장 세션 로드→`_session_valid()` 통과 시 재사용, (2) 실패 시 `POST /authentication`.

**델타 (brain_viewer `auth_login_status`, WorldQuantBrain-Agent `Check_Session_Timeout` 차용).**
1. `_session_valid()`를 `self.session.options(f'{BASE}/simulations', timeout=10)` 기반으로 교체.
   200=유효, 401=만료. **인증 엔드포인트를 아예 안 건드려** Persona/biometric throttle 재무장 위험 0.
   (GET /authentication은 inquiry를 새로 만들진 않지만, 계정/게이트웨이에 따라 상태를 흔들 수 있어
   "인증 URL을 절대 안 건드린다"는 brain_viewer 원칙이 더 안전.)
2. 선제 갱신: `GET /authentication`(검증이 아니라 **만료시각 조회 용도로만**, 최대 1회) 응답의
   `token.expiry`를 읽어 `_expiry_epoch`에 캐시. 이후 사이클은 `remaining < THRESHOLD_MIN`(예 15분)
   일 때만 재인증하고, 아니면 네트워크 호출 없이 재사용. 매 사이클 검증 호출 자체를 줄인다.
3. `wqb_data_service._house_client()`도 같은 클라이언트를 쓰므로 동일 이득.

**리스크/검증.** OPTIONS 응답코드가 계정 tier에 따라 다를 수 있음(204/403 등) → 200/2xx=유효,
401=만료, 그 외=보수적으로 "불확실→기존 GET 폴백" 이중화. 유닛테스트: OPTIONS 200/401/기타 목킹.
회귀 위험 낮음(경로 격리). **standard(브라우저) 계정엔 무관** — 아래 별도 노트.

**부록(범위 밖·향후).** standard 계정 하이브리드(브라우저 1회 생체→쿠키 하베스트→API 시뮬)는
이번에 미채택. 채택 시 `wqb_browser.py`가 로그인 후 컨텍스트 쿠키를 덤프→`WqbApiClient(session=...)`로
주입하는 seam이 이미 있으니 저비용으로 추가 가능(별도 설계).

---

## 2. 시뮬 전 로컬 사전검증  (P1)

**현재.** `presim_gate.screen()`은 **구조적/복잡도** 게이트(symbol_length·base_features·const_ratio·
structural overlap)만. `alpha_ast.validate()`는 설계철학상 **미지 식별자를 통과**(문서 line 3:
"ALLOW on any parse uncertainty; Unknown identifiers…"), operator **arity/필수 파라미터 미검증**.
즉 "존재하지 않는 필드/오타 필드/인자 수 틀린 연산자"는 **시뮬 슬롯을 쓰고 나서야** 실패한다.

**델타 (WorldQuantBrain-Agent `check_regular_formula`, WQ-Brain의 무의미 트리 스킵 차용).**
`presim_gate`에 시뮬 전 **하드 프리플라이트** 한 겹 추가(드롭 사유 명시 — no silent caps):
1. **Field 존재 검증** — `alpha_ast.fields_used(code)`의 각 필드가 **해당 genome의 (region,universe,delay)
   라이브 팔레트**(`datafield_palette`/`live_datafields.csv`)에 존재하는지. 없으면 드롭(또는 #4 리페어로 회부).
2. **Operator arity/params 검증** — `operator_catalog`(#3에서 라이브 arity 주입)로 각 호출의 인자 수·
   필수 named param(`hump=`, lookback 등)을 검사. 위반 시 드롭/리페어.
3. **Region 설정 적법성** — `settings_sweep._sanitize_settings`의 규칙을 프리플라이트에도 적용해
   불법 (universe×neutralization×delay) 조합을 시뮬 전에 컷.

**주의.** HYFE의 명시 철학은 "diversity-over-safety, 애매하면 통과"(presim_gate 주석). 이 프리플라이트는
**애매할 때 통과가 아니라, 확실히 불법(존재하지 않는 필드/arity 위반)일 때만 컷** — 오탐 시 좋은
아이디어를 죽이므로 반드시 라이브 팔레트가 최신일 때만 활성(스테일 CSV면 skip). 드롭 카운터를
`log()`/대시보드에 노출해 "몇 개를 왜 컷했는지" 항상 보이게(#7과 연동).

**검증.** 알려진 불법식(없는 필드, `hump(x,0.03)` 미수정본 등)이 프리플라이트에서 잡히고,
알려진 합법식(과거 통과 알파)은 100% 통과하는 회귀 테스트. 슬롯 절감률을 라운드 로그로 계측.

---

## 3. 라이브 `/operators` 카탈로그 + region 팔레트 강제  (P1)

**현재.** `wqb_data_service.refresh()`는 `/data-fields`만 그리드 수집. 클래스 docstring은 "/operators도"
라 하지만 **루프엔 없다** → `operator_catalog`는 정적 `brain_operators.csv`(arity/params 메타 빈약).
datafields는 region별 라이브지만, **변이/생성이 항상 그 genome의 region 팔레트만** 쓰는지는
불명확(정적 `IQC_brain_datafields.csv` 폴백 경로 존재).

**델타.**
1. `wqb_data_service.refresh()`에 `GET {BASE}/operators` 수집 추가 → `data/live_operators.csv`
   (name, category, arity/min-max inputs, 필수 params, definition). `operator_catalog`가 라이브 우선·
   정적 폴백으로 로드. 이 메타가 **#2 arity 검증**과 **변이 시 합법 자식 생성**의 단일 소스.
2. `alpha_mutate.py`(및 생성 경로)가 필드를 뽑을 때 **genome의 (region,universe,delay) 라이브 팔레트로
   스코프 고정**. delay=0이면 이미 Price-Volume로 잠기는(settings_sweep 주석) 규칙을 변이에도 관철.
3. `settings`(region/universe/delay/neutralization)를 유전자로 다룰 때 region-allowed만 샘플
   (settings_sweep의 적법조합 테이블 재사용).

**리스크.** 라이브 수집 실패/스테일 시 정적 폴백으로 degrade(하드 의존 금지). 수집 캐시 TTL은
`maybe_refresh` 패턴 재사용. operators는 region 편차가 datafields보다 작으니 갱신 빈도 낮게.

---

## 4. 가이드 리페어 연산자  (P1)

**현재.** `alpha_repair.repair()`는 **시뮬 전** 결정적 패턴 수리. 과거 실패에서 배운 패턴(drop_filter_attr,
hump_named 등)을 **하드코딩**해 넣는 방식 — 새 에러가 나오면 사람이 코드에 규칙을 추가해야 함.
`worker.py`엔 self-corr 실패값 파싱(`_extract_self_corr_value`) 등 에러 파싱 인프라가 이미 있음.

**델타 (WorldQuantBrain-Agent의 `"❌ X doesn't exist"→search_datafields` 루프 차용).**
시뮬 **실패 결과의 에러 메시지**를 표적 파싱해 **1회 자동 수리 후 재큐**(폐루프):
- `"... 'FIELD' doesn't exist"` / unknown identifier → `datafield_palette`에서 **최근접 필드**
  (편집거리 + 카테고리/타입 일치, `alpha_similarity` 재사용) 스냅.
- `Unknown attribute "X"` → 해당 attr 제거(현 `_drop_filter_named_arg`의 일반화).
- `Invalid number of inputs : N, should be exactly M` → arity 표적 수리(현 `_fix_hump_positional` 일반화).
- 수리 실패/모호 → 드롭(현행 유지). 재큐는 **알파당 1회**로 상한(무한 재시뮬 방지), 재큐 사유 로그.

**설계 포인트.** 이건 "사전 패턴"이 아니라 "**사후 에러→표적 수리**"라 커버리지가 근본적으로 넓다.
수리 성공 시 그 패턴을 통계로 쌓아, 반복되는 것은 #2 프리플라이트로 승격(에러가 시뮬까지 안 가게).

**검증.** 최근 실패 로그에서 에러 유형 분포를 뽑아, 각 유형별 수리→재시뮬 성공률을 A/B 계측.

---

## 5. 세대 내 다양성 항 (crowding/sharing)  (P2)

**현재.** novelty는 **제출풀 대비 self-corr penalty**로 `reward.py`에 **이미 완비**되고 worker에 배선됨
(`worker.py:699 compute_reward(self_corr=...)`). presim_gate엔 구조적 near-dup overlap, alpha_search엔
세대 diversity(CV) 지표가 있으나 — **부모 선택 자체엔 세대 내 다양성 압력이 없다**. reward는 per-alpha라
서로 닮은 고reward 개체 N개가 그대로 부모로 뽑혀 세대가 한 곳으로 수렴할 수 있다.

**델타 (brain_viewer `evolution._fitness`의 0.65 real + 0.35 diversity 블렌드 차용).**
부모 선택 시점에 **fitness sharing / crowding** 한 겹:
- 선택 스코어 = `reward − λ·(세대 내 최대 유사도)`. 유사도는 `alpha_similarity.jaccard`(operator+field)
  또는 `alpha_ast` 구조 overlap 재사용. λ는 `runtime.get('DIVERSITY_LAMBDA')`로 **재시작 없이** 튜닝.
- 또는 NSGA-II를 채택하면(#6) crowding-distance가 이 역할을 자연히 흡수 → **#6과 함께 설계**.

**주의.** 이미 presim overlap + self-corr penalty가 있어 과다 적용 시 throughput 저하 우려. λ 기본 낮게,
대시보드에 "세대 다양성(CV)" 추이를 띄워(#7) 효과를 보며 조정.

---

## 6. rank-백분위 composite + 파레토(NSGA-II)  (P2)

**현재.** `reward.compute_reward`는 **고정 REF**(SHARPE_REF=3.0 등) 나눗셈 정규화 → 가중합 스칼라.
장점(안정·해석 쉬움)이나, 라운드/레짐마다 분포가 달라도 척도가 고정이고, 다목적 trade-off를
단일 스칼라로 접어 **파레토 다양성 손실**.

**델타.**
1. **rank-백분위 정규화 옵션** (brain_viewer `_compute_composite_score`): 각 지표를 **그 라운드 population
   내 백분위**로 [0,1] 매핑(turnover는 뒤집기). 고정 REF 대신 분포적응. `reward.py`에 모드 플래그로
   추가하고 기존 REF 모드와 A/B(스칼라는 bandit/seeding 하위호환 유지).
2. **NSGA-II 선택층** (WQ-Brain 제안): 목적벡터 `[sharpe, fitness, −turnover, −self_corr]`로 **비지배 정렬
   + crowding distance** → 부모/생존자 선택. 스칼라 reward는 bandit arm 업데이트·seeding rank용으로 유지,
   **선택 다이내믹스만** 파레토로. 이때 #5 crowding이 자연 통합.
- 신규 모듈 `server/selection.py`(순수: non_dominated_sort, crowding_distance)로 분리, worker의 부모
  선정부만 교체. 유닛테스트 용이.

**리스크.** 선택 다이내믹스 변경은 결과에 크게 영향 → **반드시 #7(관측) 먼저** 배포해 before/after를
눈으로 확인하며 롤아웃. `runtime` 플래그로 REF-스칼라 ↔ 백분위 ↔ NSGA-II 전환 가능하게(즉시 롤백).

---

## 7. Observatory 시각화 업그레이드  (P0 — 먼저)

**현재.** `static/app.js`는 무의존 **SVG**로 `/api/recent_alphas`를 라운드(세대)로 묶어 단일지표(`_evoMetric`)
막대/라인만. 0 기준선 정도만 있음.

**델타 (brain_viewer `backtest_viewer`/`evolution` 시각화 차용 — 전부 현 무의존 SVG로 구현 가능).**
- **기준선 오버레이**: Sharpe 1.0/1.5, self-corr 0.7 게이트, turnover cap 등 합격선 vline/hline.
- **품질 계층 색상**: 막대/셀을 reward·pass 여부로 green/yellow/red 코딩.
- **하이퍼파라미터 평균 패널**: neutralization/universe/delay/decay별 평균 reward 막대(어떤 설정이 잘 먹나).
- **산점도 + 추세**: sharpe-vs-turnover, reward-vs-diversity 2D 산점도(+회귀선 r²).
- **파레토 뷰**(#6 연동): 목적 2D 평면에 비지배 프론트 하이라이트.
- **리더보드**: reward/composite 정렬 가능한 테이블(코드·지표·설정·self_corr·drop사유).
- **다양성 뷰**: `alpha_similarity` Jaccard로 근사중복 접어 "신규 후보만" 표시.
- **드롭 계측 패널**: #2/#4가 컷/수리한 개수·사유(silent cap 금지 원칙 가시화).

**왜 먼저(P0)인가.** #2·#4·#5·#6이 GA 다이내믹스를 바꾸므로, **변화를 관측할 계기판을 먼저** 세워야
before/after를 판단하고 안전하게 롤아웃한다. UI는 백엔드와 격리돼 회귀 위험도 낮다.

---

## 시퀀싱 / 롤아웃

```
P0 (즉시, 저위험·고가치·격리)
  ├─ #1 OPTIONS 헬스체크 + 선제갱신        (wqb_api.py, 유닛테스트)
  └─ #7 Observatory 계기판 업그레이드      (static/, 백엔드 무관) ← 이후 단계의 관측 기반

P1 (효율·견고성 — 슬롯/throughput 직접 이득)
  ├─ #3 라이브 /operators + region 팔레트 강제   ← #2의 전제
  ├─ #2 시뮬 전 하드 프리플라이트(field 존재·arity)  (#3 위에)
  └─ #4 가이드 리페어(사후 에러→표적 수리→재큐)

P2 (선택 다이내믹스 — 반드시 #7 관측 위에서 롤아웃, runtime 플래그로 즉시 롤백)
  ├─ #6 rank-백분위 + NSGA-II 선택층 (server/selection.py)
  └─ #5 세대 내 다양성 항 (#6에 crowding으로 통합)
```

**공통 원칙(기존 CLAUDE.md·코드 철학 준수).**
- 모든 컷/드롭/재큐는 **사유를 로그·대시보드에 노출**(no silent caps — presim_gate 철학).
- 다이내믹스 변경은 `runtime.get(...)` 플래그로 재시작 없이 on/off·튜닝, 즉시 롤백.
- 순수 로직은 신규 모듈(`selection.py` 등)로 분리해 유닛테스트(python3.11).
- KR/US·RC/standard **경로 비대칭** 주의: 변경마다 RC-API와 standard-browser 양 경로 점검.

## 미채택(이번 범위 밖) — 기록만
- standard 계정 하이브리드(브라우저→쿠키 하베스트→API), 멀티계정 세션 풀, 원격 원탭 Persona,
  Retry-After 폴링, Alpha101/reversal×volume 시딩 + 수식-해시 레지스트리, RAG-grounded 아이디에이션.
  (필요 시 후속 설계.)
