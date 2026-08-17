# GenomicWQB — 유전 알고리즘 기반 WorldQuant 알파 탐색

GenomicWQB는 알파 수식을 유전체로 표현하고, WorldQuant Brain의 시뮬레이션 결과를 적합도로 삼아 다음 세대를 만든다. LLM은 새 가설과 코드 수정을 돕지만, 세대 교체의 핵심은 선택·교차·변이·적합도 평가로 작동한다.

실행 중인 서비스: **https://iqc.ai-ve.uk**

## 평가자용 요약

| 유전 알고리즘 요소 | 구현 |
|---|---|
| 유전체 | 데이터 필드, 변환, lookback, 결합 방식, 중립화, decay, truncation 등을 타입화 |
| 적합도 | Sharpe·Fitness·Turnover·Returns·IS 통과율을 결합한 연속 선택 점수 |
| 선택 | 최근 세대의 상위 유전체를 엘리트 시드로 보존 |
| 교차 | 서로 다른 엘리트 부모의 유전자를 재조합 |
| 변이 | 수치 파라미터·연산자·필드 변이, 실패 지표에 따른 정향 변이 |
| 탐색 유지 | 기지 조합 재시뮬레이션을 막는 신규성 압력과 Thompson sampling |
| 평가·제출 | Playwright 병렬 시뮬레이션, IS 8개 검사, 제출 후 상태 재확인 |

```text
엘리트 선택 → 교차·변이 → 식 렌더링·린트 → WQB 시뮬레이션
              ↑                                      ↓
       실패 지표별 정향 변이 ← 적합도·제출 결과
```

## 문제와 해결

무작위 수식 생성만으로는 검증 비용이 크고 같은 조합이 반복된다. GenomicWQB는 통과에 가까운 식을 부모로 남기고, 실패한 검사 항목에 맞춰 변이 축을 고른다. 반면 최근 점수만 쫓아 탐색이 좁아지지 않도록 교차 파트너의 다양성과 신규성 압력을 함께 두었다.

## 전체 파이프라인

```text
유전 알고리즘으로 12개 후보 생성
  → 구조 파싱·연산자·필드 사전 검사
  → Playwright로 3개씩 병렬 시뮬레이션
  → PASS/FAIL/ERROR/PENDING 및 연속 성과지표 수집
  → 적합도 갱신·제출 시도·다음 세대 생성
```

## 누가 쓰는가

- **WQB 컨센턴트** — 알파 채굴을 24/7 무인 운영하고 싶은 사람
- **자기 알파의 self-correlation 한계를 회피하고 싶은 사람** —
  이미 제출한 코드를 Gemini 프롬프트에 명시해 다른 archetype/dataset 시도 가이드
- **여러 WQB 계정을 동시에 굴리는 팀** — 사용자별 chromium 프로필 / DB scope / 워커 쓰레드 격리

## 알아야 할 용어

| 용어 | 의미 |
|---|---|
| **Alpha** | 주가 데이터에서 미래 수익을 예측하는 수식 (예: `rank(-delta(close, 5))`) |
| **WQB / Brain** | WorldQuant Brain — 알파 시뮬레이션·채점·실거래 플랫폼 |
| **IS Tests** | In-Sample 검사. 8개 항목 (Sharpe, Turnover, Fitness, Sub-universe, Self-correlation 등) |
| **Submit** | 알파를 정식 채점 큐에 올림. Self-correlation, 제출 한도 등을 통과해야 Submitted |

## 30초 빠른 시작

```bash
# 의존성
python3 -m pip install --user --upgrade flask google-genai cryptography python-dotenv
sudo dnf install -y python3.11 python3.11-pip          # Playwright 전용 인터프리터
python3.11 -m pip install --user playwright
python3.11 -m playwright install chromium

# 기동
git clone https://github.com/Arcosium/GenomicWQB.git
cd GenomicWQB
./run.sh                # 포트 8088 foreground
# → http://localhost:8088 접속
# → 첫 화면에서 WQB 이메일/비밀번호 + Gemini API 키 입력하면 끝
```

상세 운영(systemd, Cloudflare Tunnel, Nginx, 멀티유저 운영)은 아래로 계속 읽으세요.

---

## 기능 요약

- **멀티유저** — WQB 아이디별 격리 (chromium 프로필 / 워커 스레드 / DB scope / 세션 쿠키)
- **자동 라운드** — Gemini 12 알파 생성 → Playwright 4 배치 (3 동시 시뮬) → DB 저장 → 다음 라운드
- **IS Tests scrape** — Show test period 클릭 → PASS/FAIL/ERROR/PENDING 4 섹션 자동 분류
- **Submit Alpha 자동화** — PASS≥7 충족 시 Submit Alpha 버튼 클릭 + 확인 modal 처리
- **Submit 결과 검증** — modal 닫힘 + 거절 텍스트 매칭 + post-submit 재 scrape 로 Submitted/Rejected 정확 분류
- **Self-correlation 회피** — 이미 제출 성공한 알파 코드를 Gemini prompt 에 명시 → 다른 archetype/dataset 시도 가이드
- **Auto-resume** — 서버 재시작 시 `running=1` 인 워커 자동 재기동 (사용자 클릭 불필요)
- **로그 누적 보존** — 사용자가 "화면 비우기" 누르기 전까진 SSE 재연결 / 새 디바이스 접속에도 누적 로그 복원
- **모바일 UI** — `/m` 또는 모바일 UA 자동 분기. ID/PW 만으로 로그인. 진행 상황 + 최근 제출 시도 + 시작/일시정지 버튼.
- **데스크톱 UI** — 실시간 SSE 로그 + 제출 시도 알파 표 (Submitted/Unsubmitted 배지)

## 화면 흐름

### 데스크톱 (`/`)

1. **로그인** — WQB 이메일/비밀번호 + Gemini API 키 입력. Playwright 가 실제로 WQB 사이트에 로그인 시도하고, Gemini API 키도 작은 호출로 검증.
   실패 시 다음 7가지 코드 중 하나 반환:
   - `gemini_invalid` / `gemini_quota` / `gemini_network`
   - `wqb_credentials` / `wqb_unreachable` / `wqb_captcha`
   - `wqb_auth_required` — WQB 가 새 디바이스 인증 요구 (자동화 불가, 사용자가 한 번 직접 로그인 필요)
   - `playwright_setup` — 브라우저 자동화 시작 자체 실패
2. **대시보드** — 실시간 SSE 로그 + 시작/일시정지 + 🚀 알파 제출 시도 표. `일시정지` 누르면 진행 중인 Playwright 서브프로세스도 즉시 SIGKILL.

### 모바일 (`/m`, `/mobile`, 또는 모바일 UA 로 `/` 접속)

1. **로그인** — 등록된 사용자만, ID + 비밀번호로 빠른 로그인 (Gemini 키 재입력 불필요)
2. **상태 카드 4개**:
   - 현재 라운드 (실행/일시정지 태그)
   - 완료 라운드
   - Submitted (제출 성공 누적)
   - Unsubmitted (제출 거절됨 누적)
3. **시작 / 일시정지 버튼** — 데스크톱과 동일한 워커 제어
4. **최근 제출 시도 표** — R/# / PASS / FAIL / 상태 / 시각 (시각은 KST). "화면 비우기" 로 표시 지점 이동(DB 보존)

## 디렉터리

```
/home/arcosium/projects/GenomicWQB/
├── server/                  # Flask 백엔드 + IQC 로직 (Python 3.9)
│   ├── app.py               # Flask 라우트 + SSE + auto-resume
│   ├── auth.py              # 로그인/검증 (Playwright + Gemini 호출)
│   ├── db.py                # SQLite 멀티유저 DB + 자격증명 암호화 (Fernet)
│   ├── worker.py            # 사용자별 백그라운드 워커 (threading)
│   ├── wqb_browser.py       # Playwright 자동화 (3.11 subprocess)
│   ├── gemini_strategist.py # Gemini 12 알파 생성 + anti-correlation prompt
│   ├── result_cache.py      # 알파 코드 → 캐시
│   ├── alpha_lint.py        # 화이트리스트/구조 검증
│   ├── brain_operators.csv
│   └── IQC_brain_datafields.csv
├── static/                  # 프론트엔드
│   ├── index.html           # 데스크톱 UI
│   ├── mobile.html          # 모바일 UI (inline CSS/JS)
│   ├── app.js
│   └── style.css
├── data/                    # SQLite + Fernet 키 (gitignore)
├── logs/                    # 서버 로그
├── requirements.txt
├── run.sh
└── genomicwqb.service         # systemd 유닛 예시
```

## 기동

```bash
# 의존성 (시스템 python3.9 — Flask, google-genai, cryptography)
python3 -m pip install --user --upgrade flask google-genai cryptography python-dotenv

# Playwright 는 python3.11 (browser-use 와 동일 정책)
sudo dnf install -y python3.11 python3.11-pip
python3.11 -m pip install --user playwright
python3.11 -m playwright install chromium

cd /home/arcosium/projects/GenomicWQB
./run.sh                # foreground
./run.sh background     # nohup 으로 백그라운드 (logs/server.log)
```

기본 포트 `8088`. 환경변수:
- `HYFE_IQC_HOST` — `run.sh` 기본값 `127.0.0.1` (Cloudflare 터널이 localhost 로 붙으므로 공개망 직접 노출 차단). LAN/공인 IP 직접 공개가 필요할 때만 `HYFE_IQC_HOST=0.0.0.0 ./run.sh` 로 override
- `HYFE_IQC_PORT` — 디폴트 `8088`
- `HYFE_IQC_FERNET_KEY` — Fernet 암호화 키 (미설정 시 `data/.fernet.key` 자동 생성)
- `HYFE_IQC_PASS_THRESHOLD` — Submit 시도 임계 PASS 수 (디폴트 `7`)
- `HYFE_IQC_COOKIE_SECURE` — `1` 이면 세션 쿠키에 Secure 플래그 (HTTPS 환경에서)
- `IQC_PY` — Playwright 인터프리터 경로 (디폴트 `/usr/bin/python3.11`)

## API 엔드포인트

| Path | Method | 설명 |
|---|---|---|
| `/` | GET | 모바일 UA → mobile.html, 그 외 → index.html. `?desktop=1` 강제 desktop |
| `/m`, `/mobile` | GET | mobile.html 직접 |
| `/api/login` | POST | 풀 검증 (WQB + Gemini 둘 다) → 신규 가입 또는 fast-path |
| `/api/m_login` | POST | 모바일 light 로그인 (ID/PW 만, 등록된 사용자) |
| `/api/me` | GET | 세션 검증 |
| `/api/logout` | POST | 세션 만료 |
| `/api/start` | POST | 워커 시작 |
| `/api/pause` | POST | 워커 일시정지 (서브프로세스 SIGKILL 포함) |
| `/api/status` | GET | 데스크톱용 — 현재/완료 라운드 + 오류 패턴 + 로그 ID |
| `/api/m_status` | GET | 모바일용 — 라운드 + Submitted/Unsubmitted 카운트 |
| `/api/m_submits` | GET | 데스크톱·모바일 공용 — 최근 제출 시도 목록 (현재 UI 가 사용) |
| `/api/m_submits/clear` | POST | 제출 시도 표 비우기 지점 이동 (DB 데이터 보존, 웹·모바일 공유) |
| `/api/logs` | GET | `?since=<id>&limit=<n>` 페이지네이션 |
| `/api/logs/stream` | GET | SSE 실시간 로그 |
| `/api/logs/clear` | POST | 비우기 지점만 latest 로 이동 (DB 데이터 보존) |
| `/api/m_recent` | GET | (legacy, 현재 UI 미사용) 최근 N 알파 PASS/FAIL/ERR/PEND 카운트 |
| `/api/best` | GET | (legacy, 현재 UI 미사용) Submitted + Rejected 알파 목록 |

## systemd 등록

```bash
sudo cp genomicwqb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now genomicwqb
sudo systemctl status genomicwqb
```

## Cloudflare `*.ai-ve.uk` 연결 (예: `iqc.ai-ve.uk`)

옵션 두 가지:

### A) Cloudflare Tunnel (추천 — 포트 노출 X, 가장 안전)

서버에 공인 IP 가 없거나 방화벽을 못 여는 환경에서도 동작. 무료 플랜 포함.

```bash
# 1) cloudflared 설치 (Oracle Linux 9, aarch64)
curl -fL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-aarch64.rpm -o /tmp/cloudflared.rpm
sudo dnf install -y /tmp/cloudflared.rpm

# 2) 로그인 — 브라우저 열려 ai-ve.uk 도메인 인증
cloudflared tunnel login

# 3) 터널 생성 + 라우팅
cloudflared tunnel create genomicwqb
cloudflared tunnel route dns genomicwqb iqc.ai-ve.uk

# 4) ~/.cloudflared/config.yml
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: genomicwqb
credentials-file: /home/opc/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: iqc.ai-ve.uk
    service: http://localhost:8088
  - service: http_status:404
EOF

# 5) systemd 서비스로 띄움
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

**SSE 주의** — Cloudflare Dashboard → 도메인 → Rules → Configuration Rules 에서
`iqc.ai-ve.uk` 에 대해 `Cache: Bypass` + `Disable Performance Features` 적용 권장.

### B) DNS A 레코드 + nginx 리버스 프록시

```bash
# 1) Cloudflare DNS A 레코드: iqc.ai-ve.uk → <서버 공인 IP>, Proxy: ON
# 2) nginx
sudo dnf install -y nginx
sudo tee /etc/nginx/conf.d/genomicwqb.conf > /dev/null <<'EOF'
server {
    listen 80;
    server_name iqc.ai-ve.uk;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 24h;
    proxy_send_timeout 24h;
    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
    }
}
EOF
sudo systemctl enable --now nginx
```

CF SSL 모드 = Flexible (CF→origin HTTP) 또는 Full (origin cert 설치).

## 멀티유저 동시성

서버 한 대에 여러 사용자가 각자의 WQB 자격증명으로 동시 로그인 가능.

### 격리 메커니즘

| 항목 | 격리 키 |
|---|---|
| chromium 프로필 | `~/.hyfe_iqc_browser_<sha1(wqb_username)[:10]>/` |
| 워커 thread | `worker._REGISTRY[user_id]` |
| DB 데이터 | `users / rounds / alphas / errors / feedback / logs` 모두 `user_id` 컬럼 |
| Gemini API | 각자 자기 키 (공유 가능, quota 만 공유) |
| WQB 시뮬 탭 번호 | 라벨 기반 전후 비교 (1/23/999 어떤 번호라도 동작) |
| 세션 쿠키 | 32B URL-safe 랜덤, sessions 테이블에 user_id 매핑 |

### 동시 사용자 권장 한도

- chromium subprocess 사용자당 ~400MB. OCI ARM 24GB 기준 동시 ~30명
- 한 사용자가 동시 1 라운드. `/api/start` 중복 noop

## IS Testing Status scrape 동작

WQB 의 IS Tests 패널은 4 섹션 + 8 검사 항목:

```
N PASS              ← N 개 통과
  Sharpe of 1.50 is above cutoff of 1.25.
  Turnover of 27.24% is above cutoff of 1%.
  ... (각 검사: cutoff 이상/이하)

N FAIL              ← N 개 실패
  Fitness of 0.91 is below cutoff of 1.

N ERROR             ← N 개 계산 에러 (PASS 도 FAIL 도 아님)
  Fitness check error.

N PENDING           ← N 개 보류 (보통 Self-correlation, Submit 시 검사)
  Self-correlation check pending.
```

**Submit 조건:** PASS ≥ 7 AND FAIL = 0 AND ERROR = 0. PENDING 은 허용 (Submit 시점에 새로 검사됨).

PENDING 이 Submit 시점에 새 검사 결과 FAIL 로 바뀌어 거절되는 케이스 (특히 Self-correlation 이 다른
제출된 알파와 0.7 이상 ⇒ 거절) 는 "Rejected" 로 분류:

1. modal 안 / toast / page 텍스트에 거절 메시지 매칭
   - `\d+ tests? failed` / `correlation too high` / `cannot submit` / `submission failed` 등
2. post-submit IS Tests 재 scrape — FAIL/ERROR 가 0 이 아니면 거절로 판정

## 신규 사용자 첫 로그인 안내

처음 사용하는 WQB 계정은 chromium 프로필이 비어있어서, WQB 가 다음을 요구할 수 있습니다:

1. **Cookie consent / EU GDPR 동의 banner** — 코드가 자동 dismiss 시도. 보통 자동 통과.
2. **Welcome tour / sidebar 안내 modal** — 자동 dismiss 시도. 보통 자동 통과.
3. **새 디바이스 인증 (이메일 코드 / 2FA)** — **자동화 불가**.
   - `wqb_auth_required` reason 으로 거절됨
   - 사용자가 직접 `platform.worldquantbrain.com` 에 한 번 로그인하여 인증 코드 입력
   - 인증 완료 후 GenomicWQB 에서 다시 로그인 → 자동 통과
   - 만약 chromium 프로필이 깨졌다면 다시 인증 필요

## 자격증명 보안

- 비밀번호 + Gemini API 키는 Fernet (AES-128-CBC + HMAC) 으로 암호화 후 SQLite 저장
- 마스터 키는 `HYFE_IQC_FERNET_KEY` 환경변수 또는 `data/.fernet.key` 파일 (퍼미션 600)
- 세션 토큰 32 byte URL-safe 랜덤, 7일 유효, HttpOnly 쿠키
- DB 파일 / 마스터 키 파일이 동시 유출되지 않으면 자격증명 복호화 불가
- `data/` 폴더 통째로 gitignore

## 운영 팁 / 트러블슈팅

### 워커가 자꾸 멈춤
- 서버 재시작 시 워커 thread 도 같이 죽음. **Auto-resume 으로 자동 재시작** 됨 (running=1 인 사용자에 대해 부팅 직후 워커 spawn)
- 사용자가 직접 일시정지 (paused=1) 한 경우는 자동 재개 안 됨

### "sim wait timeout" 오류 다발
- WQB sim 큐가 동시 3 슬롯을 sequential 처리할 수 있어 마지막 슬롯이 늦게 시작됨
- `PLAYWRIGHT_SIM_MAX_WAIT_SEC` 가 기본 18분으로 설정 (wqb_browser.py)
- before_metrics 캡쳐를 모든 slot click_simulate 후로 일괄 이동 → 슬롯간 baseline 오염 방지
- 매 배치 시작 시 stale tab 정리 (persistent profile 의 누적 탭)

### "is_tests body snippet" debug 다발
- IS Tests 패널이 안 잡힘 — 원인은 보통 Show test period 토글 닫힘 또는 panel 로딩 지연
- `panel already open, skip click` 검사로 토글 닫힘 방지
- 빈 결과면 4초 retry
- 다중 occurrence scoring 으로 사이드바 라벨이 아닌 진짜 패널 슬라이스 채택

### IS Tests 결과가 PASS 6/0 으로만 capping
- IS Tests scrape 가 실패하고 legacy summary metrics 기반 derive (6 항목) fallback 발동했을 가능성
- 진단: log 에 `is_tests body snippet` 라인이 떴는지 확인
- scrape 실패 다발 시 Show test period 클릭 정규식 / panel 검사 로직 확인

### 모바일 UI 가 데스크톱처럼 떠요
- `/?desktop=1` 로 강제 데스크톱 모드 (휴대폰에서)
- 또는 `/m` 직접 접속

### "Submitted" 인데 WQB 에 없어요
- modal 닫혔지만 WQB 가 silent reject 한 케이스 가능 — 거절 검증 강화됨 (post-scrape)
- 그래도 가끔 누락될 수 있으니 `https://platform.worldquantbrain.com/competitions` 에서 직접 확인 권장

## 라이선스 / 출처

- IQC 자동화 로직: 원본 ArcAI.ve/Daily/IQC 의 Python 스크립트를 별도 디렉터리로 복사
- Gemini 모델: `gemini-2.5-flash` (안정 모델), 필요 시 chain 으로 fallback
