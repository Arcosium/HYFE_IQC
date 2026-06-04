# HYFE_IQC Phase 0 — 즉시 교정 설계 (Settings-aware 캐시 · 자동수정 · 연산자 카탈로그)

> 작성 2026-06-01. 출처: `zhutoutoutousan/worldquant-miner` 분석 기반 전면개편 로드맵의 Phase 0.
> 범위 결정: **인접 P1 기반 포함** (settings 타입 컬럼 + 연산자 arity/lookback 메타데이터를 지금 추가).
> 제약: 사용자는 Pre-consultant — WQB REST API 불가. 모든 실행은 Playwright 브라우저 자동화 유지.

## 1. 목적 / 비목적

**목적**
- (P0.1) 캐시 키를 `(code_hash, settings_fingerprint)`로 확장해 **동일 코드·다른 settings 가 stale 결과를 반환하는 정확성 버그** 제거. 이후 학습 루프(P2)가 신뢰할 수 있는 결과 위에서 계산되도록 기반 확보.
- (P0.2) lint 의 drop-only 정책에 **결정적 regex 자동수정 pre-pass** 추가 — 사소한 기계적 결함(region-prefix·doubled-op·선행연산자·missing-lookback) 1회 수정으로, 구조적으로 새로운 아이디어를 typo 하나로 버리지 않음.
- (P0.3) 3곳에 흩어진 하드코딩 연산자 리스트를 **단일 `operator_catalog`** 로 통합 → `alpha_similarity` 의 op/field Jaccard 오분류 수정 + 이후 단계(AST 검증)가 참조할 메타데이터 확보.

**비목적 (이번 단계에서 안 함)**
- bandit / 진화 루프 / 보상 함수 (P2).
- `fitness/turnover/drawdown/margin` 컬럼 승격 (P2 보상 단계에서 일괄).
- AST 파서 (`alpha_ast.py`) — P1 본단계.
- `_FORBIDDEN_SUBSTRINGS` 의 error-cache 자동학습 denylist 승격 (P2). 이번엔 정적 시드 유지.
- 프롬프트로의 자동수정 피드백 ("내가 X 고쳤으니 다음엔 제대로 내라") — P2.

## 2. 정책 제약 (위반 금지)

- **diversity-over-safety**: 모든 필터·수정은 *증명 가능한* 오류만 차단/수정. 미지 식별자·새 구조는 통과시켜 시뮬 에러를 학습 캐시에 쌓는다. whitelist 회귀 금지.
- **delay0 turnover playbook**: missing-lookback 자동수정의 윈도우는 무딘 상수 금지 — delay-aware.
- **워커 라운드경계 재시작**: Phase 0 자체는 라운드 내 상태를 새로 안 만들지만, 저장은 알파 insert 시점에 즉시 반영(기존 패턴 유지).
- **커밋 규율**: 사장 명시 요청 시에만 커밋.

## 3. 공통 기반 — DB 마이그레이션 (`server/db.py`)

`_SCHEMA_VERSION` `1 → 2`. 기존 `user_version` 게이트(db.py:263) 안에 멱등 ALTER 추가.

`alphas` 테이블 신규 컬럼 (전부 nullable, 기존 행 NULL):
| 컬럼 | 타입 | 출처 |
|---|---|---|
| `region` | TEXT | effective settings |
| `universe` | TEXT | effective settings |
| `delay` | INTEGER | forced_delay |
| `neutralization` | TEXT | effective settings |
| `decay` | INTEGER | effective settings |
| `truncation` | REAL | effective settings |
| `settings_fp` | TEXT | `settings_fingerprint(eff)` |
| `self_corr` | REAL | scrape (PASS≥6), 없으면 NULL |

인덱스: `CREATE INDEX IF NOT EXISTS idx_alphas_hash_fp ON alphas(code_hash, settings_fp)`.

마이그레이션은 `PRAGMA table_info(alphas)` 로 컬럼 존재 확인 후 `ADD COLUMN` (기존 phase 컬럼 추가 패턴과 동일). 백필 없음(기존 행은 universe/neut 미저장이라 복원 불가 → NULL 유지가 정확).

## 4. P0.1 — settings-aware 캐시 키

### 4.1 effective settings + fingerprint
새 함수 (위치: `server/db.py` 또는 신규 `server/settings_fp.py` — 구현 계획에서 확정):

```
WQB_DEFAULTS = {region:'USA', universe:'TOP3000', neutralization:'INDUSTRY',
                decay:'0', truncation:'0.01', pasteurization:'ON', nan_handling:'OFF'}

def effective_settings(partial: dict, forced_delay: str|int) -> dict:
    # WQB_DEFAULTS 위에 partial 덮어쓰고 delay 는 forced_delay 로 강제 주입.
    # 모든 값 string 정규화 (소수/대문자 통일). 반환은 정렬 가능한 정규 dict.

def settings_fingerprint(eff: dict) -> str:
    # 정렬된 key=value 직렬화 → sha256 → [:16].
```

규칙:
- `delay` 는 항상 `forced_delay` 에서 옴 (Gemini settings 의 delay 무시). → 기존 `metrics._delay` ad-hoc 비교를 흡수.
- 부분 dict 과 기본값-동치 dict 는 **같은 fingerprint** (`{universe:TOP3000}` ≡ `{}`).
- `truncation`/`decay` 는 숫자 정규화(`0.01`==`0.010`, `4`==`4.0`→`4`).

### 4.2 조회 / 저장
- `lookup_alpha_by_hash(user_id, h, settings_fp=None)`: `settings_fp` 주어지면 `WHERE code_hash=? AND settings_fp=?`; None 이면 기존 동작(하위호환).
- **cross-user 공유 유지** (user_id 필터 안 함 — 기존 의도). 티어별 데이터 접근 차이로 동일 code+settings 가 사용자별 다른 결과를 낼 위험은 *알려진 한계*로 주석 명시.
- `result_cache.lookup(user_id, code, settings_fp)` 시그니처 확장.
- `insert_alpha`: alpha['settings'] + forced_delay 로 effective 계산 → settings 컬럼 + settings_fp 채움. (worker 가 alpha dict 에 settings·forced_delay 전달.)

### 4.3 worker 통합 (`server/worker.py:318-326`)
```
eff = effective_settings(s.get('settings') or {}, forced_delay)
fp  = settings_fingerprint(eff)
cached = result_cache.lookup(self.user_id, s['code'], fp)   # fp 매칭
# (기존 metrics._delay 동등 비교 블록 삭제 — fp 가 delay 포함)
```
기존 행(settings_fp NULL)은 non-null fp 와 안 맞음 → miss → 재시뮬. 이는 의도된 정확성 비용.

## 5. P0.2 — 자동수정 pre-pass (`server/alpha_repair.py` 신규)

```
def repair(code: str, *, delay: int|str) -> tuple[str, list[str]]:
    # 순서 있는 안전 수정. (repaired_code, applied_fix_labels) 반환.
```

수정 패스 (순서대로, 각 적용 시 라벨 기록):
1. **region-prefix strip**: `\b(USA|EUR|EUROPE|CHN|CHINA|ASI|GLB|JPN|KOR|TWN|HKG|AMR)\.([A-Za-z_]\w*)` → `\2`.
2. **doubled-operator collapse**: 연산자 카탈로그의 op 이 바로 자신+`(` 형태로 중복될 때만 (`\b(op)\1\s*\(` → `\1(`). 일반 토큰 무차별 collapse 금지.
3. **선행 연산자 제거**: `^\s*[+*]\s*` → ``.
4. **missing-lookback (delay-aware)**: 카탈로그가 `needs_lookback` 으로 표시한 op 이 `op( <단순인자> )` 형태(콤마 없음, 단순 인자=식별자 또는 단일 비콤마 토큰)로 호출되면 `,W` 추가.
   - `W = 22 if int(delay)==0 else 10` (config 가능; delay0 은 턴오버 억제 위해 큰 윈도우).
   - 중첩/콤마 모호하면 손대지 않음.

통합 (`gemini_strategist._filter_by_lint`, gemini_strategist.py:653):
```
for s in strategies:
    fixed, applied = alpha_repair.repair(s['code'], delay=forced_delay)
    if applied:
        s['code'] = fixed                 # 코드 교체
        log_fn(f"#{idx} 자동수정: {applied}")  # 로그(프롬프트 피드백은 P2)
    bads = _alpha_violations(s['code']) or alpha_lint.validate_alpha(s['code'])
    ...  # 수정 후 재검증, 통과시 clean
```
`_filter_by_lint` 가 `forced_delay` 를 받도록 시그니처 확장(현재 caller 가 이미 forced_delay 보유).

의미 보존: 위 4개 명시 케이스만. 모호하면 미수정→기존 drop. 멱등(두 번 돌려도 동일).

## 6. P0.3 — 연산자 카탈로그 통합 (`server/operator_catalog.py` 신규)

```
# brain_operators.csv (name,category,description; 73행) 로드 → 단일 카탈로그.
OPERATORS: dict[str, OpMeta]        # name -> {category, needs_lookback, scope, desc}
def operator_names() -> frozenset[str]
def is_operator(tok: str) -> bool
def needs_lookback(name: str) -> bool
```
휴리스틱 (advisory — 설명이 짧아 hard-gate 안 함):
- `name.startswith('ts_')` → `needs_lookback=True`.
- `name.startswith('vec_')` → `scope='VECTOR'`.
- category 는 CSV 그대로.
- (선택) description 에 'at least'/콤마 힌트 있으면 arity 보조 추출 — 없으면 미설정.

소비처 리팩터:
- `alpha_similarity._KNOWN_OPS` → `operator_catalog.operator_names()` (extract_operators/extract_fields 가 카탈로그 기준 → **op 을 field 로 오분류하던 버그 수정**).
- `db.operator_preference_stats` 의 KNOWN_OPS → 카탈로그.
- `gemini_strategist._FORBIDDEN_SUBSTRINGS`(denylist, `_alpha_violations` 에서 사용)는 **연산자-이름 카탈로그와 별개** — 정적 시드로 그대로 유지(자동학습 승격은 P2). 이번 통합 대상은 op-name 집합 3곳(`_KNOWN_OPS`×2 + 신규 카탈로그)뿐.

파싱 실패/CSV 부재 시 graceful: 현재 하드코딩 리스트를 fallback 시드로 내장(무회귀).

## 7. 테스트 (`python3.11 -m pytest`)

- `tests/test_settings_fp.py`: fingerprint 결정성 · 기본값 동치(`{universe:TOP3000}`≡`{}`) · 숫자 정규화 · delay 주입 · settings 다르면 다른 fp.
- `tests/test_settings_cache.py`: 같은 code+같은 fp → hit, 다른 fp → miss, 기존 행(NULL fp) → miss.
- `tests/test_alpha_repair.py`: 4개 패스 각각 + 멱등성 + delay-aware 윈도우(0→22,1→10) + 의미 비변경(중첩/모호 미수정) + 정상 코드 무변경.
- `tests/test_operator_catalog.py`: CSV 로드 · ts_/vec_ 휴리스틱 · `is_operator` parity(기존 _KNOWN_OPS 항목 모두 인식) · CSV 부재 fallback.
- `tests/test_db_migration.py`(또는 기존 db 테스트 확장): user_version 1→2 멱등, 신규 컬럼 존재, 기존 행 NULL settings_fp.

## 8. 반영 / 롤아웃

- 모듈 변경 → **서버 재시작 필요** (`sudo systemctl restart hyfe-iqc.service`).
- 캐시 키 변경으로 기존 캐시 히트율 일시 하락(재시뮬 증가) — 의도된 정확성 비용, 라운드 진행하며 새 키로 재축적.
- 커밋: 사장 명시 요청 시에만.

## 9. 위험 / 완화

| 위험 | 완화 |
|---|---|
| 마이그레이션이 라이브 WAL DB 손상 | 멱등 ADD COLUMN, user_version 게이트, 백필 없음, 신규 컬럼 nullable |
| fingerprint 가 "요청 settings"와 "실제 적용 settings" 불일치 | effective_settings 로 기본값 채움; 단 apply_settings 가 UI 에서 silent fallback 하는 경우는 P3(적용값 캡처)로 별도 — 이번엔 요청값 기준 명시 |
| 자동수정이 의미 변경 | 4개 명시 케이스만, 모호시 미수정, 멱등, delay-aware 윈도우 |
| 카탈로그 arity 휴리스틱 오류 | advisory only(hard-gate 아님), CSV 부재시 하드코딩 fallback |
| cross-user 캐시 + 티어 차이 | 알려진 한계로 문서화; settings_fp 가 대부분의 오염 제거 |

## 10. 영향 파일

- `server/db.py` — 마이그레이션, code_hash 유지, lookup_alpha_by_hash(+settings_fp), insert_alpha(+컬럼), (effective_settings/settings_fingerprint 여기 또는 신규 모듈)
- `server/result_cache.py` — lookup(+settings_fp) · materialize
- `server/worker.py` — 캐시 조회부(318-326) fp 매칭, _filter_by_lint 에 forced_delay 전달
- `server/gemini_strategist.py` — _filter_by_lint(+forced_delay, repair 호출)
- `server/alpha_repair.py` — 신규
- `server/operator_catalog.py` — 신규
- `server/alpha_similarity.py` — _KNOWN_OPS → 카탈로그
- `tests/` — 위 5개 테스트 파일
