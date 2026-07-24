"""HYFE_IQC 멀티유저 SQLite DB.

스키마:
  users      — WQB 자격증명(암호화) + Gemini API 키(암호화) + 라운드 카운터
  sessions   — 로그인 토큰 (쿠키)
  rounds     — user_id 별 라운드 (number, status, started/ended)
  alphas     — round 안의 알파 1개당 한 row (코드, pass_count, metrics, error)
  errors     — user_id 별 정규화된 오류 패턴 (Gemini 회피 가이드용)
  feedback   — user_id 별 누적 피드백 (Gemini 학습 시드, FIFO 30개)
  logs       — user_id 별 라운드 로그 한 줄 (SSE 스트림 + 영구 저장)

자격증명 암호화: cryptography.fernet (AES-128-CBC + HMAC).
마스터 키는 환경변수 `HYFE_IQC_FERNET_KEY` 에 보관 (없으면 .secret_key 파일 자동 생성).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import operator_catalog as _operator_catalog
from . import settings_fp as _settings_fp

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('HYFE_DB_PATH') or os.path.abspath(
    os.path.join(_THIS_DIR, '..', 'data', 'hyfe_iqc.db')
)
SECRET_KEY_PATH = os.path.abspath(os.path.join(_THIS_DIR, '..', 'data', '.fernet.key'))

_DB_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()
_INITIALIZED = False

# 스키마/데이터 마이그레이션 버전. ALTER·백필을 프로세스마다 재실행하지 않도록
# PRAGMA user_version 게이트의 기준값. 향후 스키마/데이터 마이그레이션을
# 추가하면 반드시 이 값을 올려야 새 마이그레이션이 1회 적용된다.
_SCHEMA_VERSION = 7   # v7: 리서치→가설→전략스펙 파이프라인 + users.backend(능력 기반 백엔드)
_FERNET: Fernet | None = None

FEEDBACK_CAP = 30
SESSION_TTL_SEC = 7 * 24 * 3600  # 7일
SPACE_RX = re.compile(r'\s+')


def _column_missing(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """테이블에 col 컬럼이 없으면 True (마이그레이션 가드용)."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return col not in cols


def _coerce_float_or_none(v):
    """metrics 값(숫자/숫자문자열)을 float 로, 그 외(None/bool/비숫자문자열)는 None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _with_conn(fn):
    """init() + _DB_LOCK + _connect() 보일러플레이트 데코레이터.

    *전체 본문이 정확히* `init(); with _DB_LOCK, _connect() as conn: ...` 인
    함수에만 적용 — 래퍼가 그 셋업을 똑같이 수행하고 conn 을 첫 인자로 주입.
    연결 종료 후 추가 작업이 있는 함수에는 적용하지 않는다(의미가 달라짐).
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        init()
        with _DB_LOCK, _connect() as conn:
            return fn(conn, *args, **kwargs)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# 제출 판정 — 단일 진실 공급원 (single source of truth)
#
# 사용자 정책: 알파가 "제출 안 됨" 으로 간주되는 유일한 경우는
#   ┌ Submit 후 Self-correlation 테스트가 실제로 돌았고
#   ├ Failed 가 떠서 7 Pass / 1 Fail 로 바뀌었으며
#   └ 구체적인 self-correlation 수치(예: 0.9415, ≥ 0.7)가 잡힌
# 케이스뿐이다. 그 외 모든 결과 — 무응답(no_response_modal_less),
# 'Cannot submit', 예외, 버튼 비활성, 수치 없는 'above cutoff' 등 — 는
# 전부 제출된 것으로 간주한다. WQB 는 제출을 self-corr 로 거절할 때만
# 구체 수치를 노출하므로, 그 신호의 부재 == 사실상 제출 성공이기 때문.
# ─────────────────────────────────────────────────────────────────────────────

# 정책 권위 = genuine_selfcorr_reject() (행 단위 판정·insert_alpha 가 사용).
# 아래 _GENUINE_SELFCORR_SQL 은 bulk count/migration 쿼리용 *동일 규칙의 SQL
# 미러* 일 뿐이다 (rejected: 접두 + 'correlation' 언급 + 소수값). 한쪽을
# 고치면 반드시 다른 쪽도 함께 고치고 양자 일치를 검증할 것.
_SELFCORR_NUM_RE = re.compile(r'\d+\.\d+')
_GENUINE_SELFCORR_SQL = (
    "submit_status LIKE 'rejected:%' "
    "AND LOWER(submit_status) LIKE '%correlation%' "
    "AND submit_status GLOB '*[0-9].[0-9]*'"
)


def genuine_selfcorr_reject(submit_status: str) -> bool:
    """submit_status 가 '구체 수치를 동반한 Self-correlation 거절' 인가.

    True 인 경우에만 알파를 미제출로 취급한다 — 그 외는 전부 제출 간주.
    """
    s = (submit_status or '').strip()
    if not s.lower().startswith('rejected:'):
        return False
    body = s[len('rejected:'):]
    if 'correlation' not in body.lower():
        return False
    return _SELFCORR_NUM_RE.search(body) is not None


# 제출이 **일어나지 않았음**이 확실한 상태 접두사. API 백엔드는 성공 시 정확히
# 'submitted' 를 돌려주므로(wqb_api.submit_alpha), 이들은 전부 미제출이다.
_NOT_SUBMITTED_PREFIXES = (
    'submit_skipped:',        # 우리 게이트가 막음 (일일 예산·품질 문턱)
    'submit_error:',          # 네트워크/인증 실패
    'submit_pending_timeout:',
    'rejected:',              # WQB 가 거절 (체크 미달)
    'submit_http_',           # 403/401/502 — 응답 자체가 실패
    'skip_star:',             # all-pass 아니라 시도조차 안 함
    'fail:',
)
# 위 접두사의 SQL 판정식 (마이그레이션에서 재사용).
_NOT_SUBMITTED_SQL = ' OR '.join(
    f"TRIM(submit_status) LIKE '{p}%'" for p in _NOT_SUBMITTED_PREFIXES)


def effectively_submitted(submitted: Any, submit_status: str) -> bool:
    """이 알파를 제출 성공으로 간주할지.

    ⚠ 2026-07-21 수정. 원래는 '상태값이 비어있지 않고 self-corr 거절이 아니면 제출'
      이라는 **브라우저 시대 추정**이었다. 그땐 제출 성공 여부를 확실히 알 방법이 없어
      낙관적으로 셌지만, REST API 백엔드는 성공을 정확히 'submitted' 로 알려준다.
      그 추정을 그대로 두면 오늘 신설한 `submit_skipped:below_value(...)` 처럼
      **제출한 적 없는 알파가 제출됨으로 기록된다** (라이브에서 8건 오기록 확인).
      하루 4건뿐인 제출을 세는 화면이 거짓말을 하면 판단이 통째로 어긋난다.
    """
    s = (submit_status or '').strip()
    if not s:
        # 시도 자체가 없었음(sim 에러 등) — 플래그를 그대로 따른다.
        return bool(submitted)
    if s.startswith(_NOT_SUBMITTED_PREFIXES):
        return False
    return not genuine_selfcorr_reject(s)


def init() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        _ensure_fernet()
        with _DB_LOCK, _connect() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wqb_username TEXT UNIQUE NOT NULL,
                wqb_password_enc TEXT NOT NULL,
                gemini_api_key_enc TEXT NOT NULL,
                last_round_num INTEGER NOT NULL DEFAULT 0,
                running INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                last_login_at REAL NOT NULL,
                last_validated_at REAL NOT NULL DEFAULT 0,
                submittable_list_created INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_num INTEGER NOT NULL,
                status TEXT NOT NULL,         -- generating | simulating | done | error | paused
                started_at REAL NOT NULL,
                ended_at REAL,
                pass_count INTEGER NOT NULL DEFAULT 0,
                err_count INTEGER NOT NULL DEFAULT 0,
                cache_hits INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_rounds_user ON rounds(user_id, round_num);

            CREATE TABLE IF NOT EXISTS alphas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_id INTEGER NOT NULL,
                round_num INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                code TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                desc TEXT NOT NULL DEFAULT '',
                pass_count INTEGER NOT NULL DEFAULT 0,
                pass_items TEXT NOT NULL DEFAULT '[]',
                fail_count INTEGER NOT NULL DEFAULT 0,
                fail_items TEXT NOT NULL DEFAULT '[]',
                error_text TEXT NOT NULL DEFAULT '',
                metrics TEXT NOT NULL DEFAULT '{}',
                mode TEXT NOT NULL DEFAULT '',
                cached INTEGER NOT NULL DEFAULT 0,
                submitted INTEGER NOT NULL DEFAULT 0,
                submit_status TEXT NOT NULL DEFAULT '',
                error_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                ts REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(round_id) REFERENCES rounds(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_alphas_user_hash ON alphas(user_id, code_hash);
            CREATE INDEX IF NOT EXISTS idx_alphas_round ON alphas(round_id);

            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                identifiers TEXT NOT NULL DEFAULT '[]',
                sample_code TEXT NOT NULL DEFAULT '',
                first_round INTEGER NOT NULL DEFAULT 0,
                last_round INTEGER NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                UNIQUE(user_id, pattern),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_errors_user ON errors(user_id);

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_num INTEGER NOT NULL,
                payload TEXT NOT NULL,        -- json blob (code/desc/pass_count/...)
                ts REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, id);

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_num INTEGER NOT NULL,
                ts REAL NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                line TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, id);

            CREATE TABLE IF NOT EXISTS submit_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                round_num INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                code TEXT NOT NULL DEFAULT '',
                submitted INTEGER NOT NULL DEFAULT 0,
                submit_status TEXT NOT NULL DEFAULT '',
                pass_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                ts REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_submit_attempts_user
                ON submit_attempts(user_id, id);

            CREATE TABLE IF NOT EXISTS bandit_arms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                arm_key TEXT NOT NULL,
                dimension TEXT NOT NULL DEFAULT '',
                reward_sum REAL NOT NULL DEFAULT 0,
                reward_sq_sum REAL NOT NULL DEFAULT 0,
                visits INTEGER NOT NULL DEFAULT 0,
                last_round INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                UNIQUE(user_id, arm_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_bandit_user_dim ON bandit_arms(user_id, dimension);

            -- ── v7: 리서치 → 가설 → 전략스펙 (LLM 파이프라인) ──────────────────
            -- 사용자가 요청을 넣으면 research_runs 1건이 생기고, Arachne 근거 수집 →
            -- LLM 가설 N개(hypotheses) → 가설마다 타입드 유전체 후보 K개(strategy_specs).
            -- 스펙은 GA 의 '초기 개체'로 소비된다(1회성). 요청이 없으면 이 테이블들은
            -- 비어 있고 워커는 기존 무작위 GA 로 그대로 돈다.
            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                    -- pending|gathering|ideating|concretizing|ready|error
                evidence TEXT NOT NULL DEFAULT '',   -- [출처N] 번호매김 근거 블록
                sources TEXT NOT NULL DEFAULT '[]',  -- [{"n":1,"title":..,"url":..}]
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_research_user ON research_runs(user_id, id);

            CREATE TABLE IF NOT EXISTS hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                citations TEXT NOT NULL DEFAULT '[]',   -- 인용한 출처 번호 [1,4]
                family_hint TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_hypo_run ON hypotheses(run_id);

            CREATE TABLE IF NOT EXISTS strategy_specs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hypothesis_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                genome TEXT NOT NULL,                  -- 검증된 타입드 유전체 JSON
                code TEXT NOT NULL,                    -- render(genome) 스냅샷
                settings TEXT NOT NULL DEFAULT '{}',
                delay INTEGER,                         -- 스펙이 원하는 delay (0/1) or NULL
                status TEXT NOT NULL DEFAULT 'pending',
                    -- pending|seeded|exhausted|rejected
                seeded_round INTEGER,
                alpha_id INTEGER,                      -- 이 스펙이 낳은 알파 행
                why TEXT NOT NULL DEFAULT '',          -- 이 후보를 만든 이유(LLM)
                created_at REAL NOT NULL,
                FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_specs_user_status
                ON strategy_specs(user_id, status, id);
            ''')

            # ── 스키마/데이터 마이그레이션 (PRAGMA user_version 게이트) ──
            # CREATE TABLE IF NOT EXISTS 는 신규 DB 위해 항상 실행하되, ALTER·백필은
            # 프로세스마다 재실행할 필요가 없어 user_version < _SCHEMA_VERSION 일 때 1회만.
            _ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if _ver < _SCHEMA_VERSION:
                # 마이그레이션 — 기존 DB 에 신규 컬럼 없으면 추가.
                cols = {r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
                if 'submittable_list_created' not in cols:
                    conn.execute(
                        'ALTER TABLE users ADD COLUMN submittable_list_created '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'last_cleared_log_id' not in cols:
                    # 사용자가 마지막으로 "화면 비우기" 한 시점의 log id. SSE 첫 연결 시
                    # 이 id 이후의 로그를 전부 replay → 새로고침/재접속해도 누적 로그 유지.
                    conn.execute(
                        'ALTER TABLE users ADD COLUMN last_cleared_log_id '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'last_cleared_submit_id' not in cols:
                    # 사용자가 마지막으로 "제출 시도 화면 비우기" 한 시점의
                    # submit_attempts.id. 모바일 제출 시도 목록은 이 id 초과만 노출.
                    conn.execute(
                        'ALTER TABLE users ADD COLUMN last_cleared_submit_id '
                        'INTEGER NOT NULL DEFAULT 0'
                    )

                # alphas 테이블에 신규 컬럼 마이그레이션 — 기존 DB 도 호환.
                alpha_cols = {r['name'] for r in conn.execute("PRAGMA table_info(alphas)").fetchall()}
                if 'submitted' not in alpha_cols:
                    conn.execute(
                        'ALTER TABLE alphas ADD COLUMN submitted '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'submit_status' not in alpha_cols:
                    conn.execute(
                        "ALTER TABLE alphas ADD COLUMN submit_status TEXT NOT NULL DEFAULT ''"
                    )
                if 'error_count' not in alpha_cols:
                    conn.execute(
                        'ALTER TABLE alphas ADD COLUMN error_count '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'pending_count' not in alpha_cols:
                    conn.execute(
                        'ALTER TABLE alphas ADD COLUMN pending_count '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'phase' not in alpha_cols:
                    # focused sub-round 번호. 0 = 메인 라운드, 1+ = N번째 sub-round.
                    conn.execute(
                        'ALTER TABLE alphas ADD COLUMN phase '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                # Phase 0: settings 타입 컬럼 + 캐시 fingerprint + self_corr (전부 nullable;
                # 기존 행은 universe/neut 미저장이라 NULL 유지가 정확 — 백필 없음).
                for _c, _decl in (
                    ('region', 'TEXT'), ('universe', 'TEXT'), ('delay', 'INTEGER'),
                    ('neutralization', 'TEXT'), ('decay', 'INTEGER'),
                    ('truncation', 'REAL'), ('settings_fp', 'TEXT'), ('self_corr', 'REAL'),
                ):
                    if _c not in alpha_cols:
                        conn.execute(f'ALTER TABLE alphas ADD COLUMN {_c} {_decl}')
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_alphas_hash_fp '
                    'ON alphas(code_hash, settings_fp)'
                )
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_alphas_submitted '
                    'ON alphas(user_id, submitted)'
                )
                # Phase 1: 지표 컬럼(reward/통계 쿼리용) + 진화 lineage. 전부 nullable.
                # genome = 생성 시점의 유전체 JSON. 이게 없으면 시딩이 코드에서 정규식으로
                # 유전체를 역추출해야 하는데, 그건 손실 압축이라 자식이 부모를 복제조차 못 한다
                # (2026-07-11 진단). genome IS NULL = 유전체 미보유 = 시드 자격 없음.
                for _c, _decl in (
                    ('sharpe', 'REAL'), ('fitness', 'REAL'), ('turnover', 'REAL'),
                    ('drawdown', 'REAL'), ('margin', 'REAL'), ('returns', 'REAL'),
                    ('generation', 'INTEGER'), ('parent_alpha_id', 'INTEGER'),
                    ('genome', 'TEXT'),
                ):
                    if _c not in alpha_cols:
                        conn.execute(f'ALTER TABLE alphas ADD COLUMN {_c} {_decl}')

                # v6: 변이 귀속(attribution) — 어떤 부모에 어떤 변이 축을 적용해 이
                # 자식이 나왔는지. directive_stats() 가 (fail category × directive)
                # 성공률 행렬로 집계해 정향변이의 Thompson sampling 에 먹인다.
                for _c, _decl in (
                    ('origin', 'TEXT'),          # random | mutate | crossover
                    ('directive', 'TEXT'),        # smooth/sharpen/... (mutate 일 때만)
                    ('genes_changed', 'TEXT'),    # 부모 대비 바뀐 유전자명 JSON 리스트
                ):
                    if _c not in alpha_cols:
                        conn.execute(f'ALTER TABLE alphas ADD COLUMN {_c} {_decl}')
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_alphas_parent '
                    'ON alphas(user_id, parent_alpha_id)'
                )

                # v7: 이 알파를 낳은 전략스펙(LLM 파이프라인 산출물). NULL = 순수 GA 산.
                if 'spec_id' not in alpha_cols:
                    conn.execute('ALTER TABLE alphas ADD COLUMN spec_id INTEGER')

                # 데이터 마이그레이션 (idempotent) — Submit 클릭이 발생했으나
                # '구체 수치 동반 self-corr 거절' 이 아닌 모든 알파를 제출 성공으로 정정.
                # 과거에 fail:no_response_modal_less / rejected:Cannot submit 로
                # 잘못 미제출 처리된 행들을 한 번에 바로잡는다. 진짜 self-corr
                # 거절(수치 포함)만 submitted=0 으로 남는다. 이미 1 인 행은 무변화.
                # ⚠ 2026-07-21: 미제출이 확실한 상태값은 제외한다. 이 조건이 없으면
                #   기동할 때마다 submit_skipped:/rejected: 행을 제출 성공으로 되돌려
                #   effectively_submitted 수정이 무효화된다.
                conn.execute(
                    'UPDATE alphas SET submitted=1 '
                    "WHERE submitted=0 AND TRIM(submit_status) <> '' "
                    f'AND NOT ({_GENUINE_SELFCORR_SQL}) '
                    f'AND NOT ({_NOT_SUBMITTED_SQL})'
                )

                # rounds 테이블 마이그레이션 — focused sub-round 메타.
                round_cols = {r['name'] for r in conn.execute("PRAGMA table_info(rounds)").fetchall()}
                if 'phase' not in round_cols:
                    conn.execute(
                        'ALTER TABLE rounds ADD COLUMN phase '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'parent_idx' not in round_cols:
                    conn.execute(
                        'ALTER TABLE rounds ADD COLUMN parent_idx '
                        'INTEGER NOT NULL DEFAULT 0'
                    )
                if 'focus_fail' not in round_cols:
                    conn.execute(
                        "ALTER TABLE rounds ADD COLUMN focus_fail TEXT NOT NULL DEFAULT ''"
                    )
                # Phase 1: rounds 설정 스냅샷 컬럼 (Phase 2 evolution에서 채움; nullable).
                for _c, _decl in (
                    ('delay_mode', 'TEXT'), ('gen_temperature', 'REAL'),
                    ('explore_exploit', 'TEXT'), ('injected_arms', 'TEXT'),
                ):
                    if _c not in round_cols:
                        conn.execute(f'ALTER TABLE rounds ADD COLUMN {_c} {_decl}')

                # users 테이블 — 다음 라운드에서 처리할 focused sub-round 큐.
                user_cols = {r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
                if 'focus_queue' not in user_cols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN focus_queue TEXT NOT NULL DEFAULT '[]'"
                    )

                # v4: 계정 유형 (standard=브라우저, research_consultant=공식 API)
                if _column_missing(conn, 'users', 'account_type'):
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'standard'"
                    )

                # v7: 시뮬 백엔드 = **측정된 능력**('api'), 역할(account_type)과 분리한다.
                # ''(미탐침) → 첫 로그인/워커 기동 때 POST /authentication 으로 1회 탐침.
                # RC 는 오늘 이미 API 로 도는 게 증명돼 있으므로 그대로 백필한다.
                # ⚠ 반드시 account_type ALTER **뒤에** 와야 한다 — 신규 DB 는 이 시점에야
                #   account_type 컬럼이 존재한다(아래 UPDATE 가 그걸 참조한다).
                if _column_missing(conn, 'users', 'backend'):
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN backend TEXT NOT NULL DEFAULT ''")
                    conn.execute(
                        "UPDATE users SET backend='api' "
                        "WHERE account_type='research_consultant'")

                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        _INITIALIZED = True


def get_submittable_list_created(user_id: int) -> bool:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT submittable_list_created FROM users WHERE id=?',
                           (user_id,)).fetchone()
    return bool(row and int(row['submittable_list_created'] or 0))


@_with_conn
def set_submittable_list_created(conn, user_id: int, value: bool = True) -> None:
    conn.execute('UPDATE users SET submittable_list_created=? WHERE id=?',
                 (1 if value else 0, user_id))


def get_last_cleared_log_id(user_id: int) -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT last_cleared_log_id FROM users WHERE id=?',
                           (user_id,)).fetchone()
    return int(row['last_cleared_log_id'] or 0) if row else 0


def set_last_cleared_log_id(user_id: int, log_id: int) -> int:
    """비우기 지점을 앞으로만 이동 (뒤로 절대 가지 않음)."""
    init()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            'UPDATE users SET last_cleared_log_id=MAX(last_cleared_log_id, ?) '
            'WHERE id=?',
            (int(log_id or 0), user_id),
        )
        row = conn.execute('SELECT last_cleared_log_id FROM users WHERE id=?',
                           (user_id,)).fetchone()
    return int(row['last_cleared_log_id'] or 0) if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# 제출 시도 (submit_attempts) — 알파 제출 시도를 라운드 종료를 기다리지 않고
# 발생 즉시 기록. 모바일 대시보드가 '최근 제출 시도' 를 실시간 열람.
# ─────────────────────────────────────────────────────────────────────────────

@_with_conn
def record_submit_attempt(conn, user_id: int, round_num: int, idx: int, code: str,
                           submitted: Any, submit_status: str,
                           pass_count: int = 0, fail_count: int = 0) -> None:
    """Submit 버튼 클릭이 발생한 알파 1건 — 발생 즉시 호출 (worker _on_partial)."""
    conn.execute(
        'INSERT INTO submit_attempts (user_id, round_num, idx, code, '
        'submitted, submit_status, pass_count, fail_count, ts) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (user_id, int(round_num or 0), int(idx or 0), code or '',
         1 if submitted else 0, str(submit_status or ''),
         int(pass_count or 0), int(fail_count or 0), time.time()),
    )


def submitted_today(user_id: int, now: float | None = None) -> int:
    """오늘(EST 기준) **성공한** 제출 건수. 일일 제출 예산(4건) 집행에 쓴다.

    WQB 컨설턴트는 하루 최대 4개까지만 제출할 수 있다(Power Pool 문서: "Max 4 alpha
    submissions in a day"). 리셋은 EST 자정 기준이므로 UTC-5 로 환산해 날짜를 자른다
    (서머타임은 무시 — 경계에서 한 시간 어긋나도 예산이 하루 밀릴 뿐 초과 제출은 없다).
    """
    import datetime as _dt
    ts = time.time() if now is None else float(now)
    est = _dt.datetime.utcfromtimestamp(ts) - _dt.timedelta(hours=5)
    midnight_est = est.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (midnight_est + _dt.timedelta(hours=5)).replace(
        tzinfo=_dt.timezone.utc).timestamp()
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM submit_attempts '
            'WHERE user_id=? AND submitted=1 AND ts>=?', (user_id, start)).fetchone()
    return int(row['n'] or 0) if row else 0


def latest_submit_id(user_id: int) -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT MAX(id) AS m FROM submit_attempts WHERE user_id=?',
            (user_id,)).fetchone()
    return int(row['m'] or 0) if row else 0


def get_last_cleared_submit_id(user_id: int) -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT last_cleared_submit_id FROM users WHERE id=?',
                           (user_id,)).fetchone()
    return int(row['last_cleared_submit_id'] or 0) if row else 0


def set_last_cleared_submit_id(user_id: int, submit_id: int) -> int:
    """비우기 지점을 앞으로만 이동 (로그 비우기와 동일 의미 — 데이터는 보존)."""
    init()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            'UPDATE users SET last_cleared_submit_id=MAX(last_cleared_submit_id, ?) '
            'WHERE id=?',
            (int(submit_id or 0), user_id),
        )
        row = conn.execute('SELECT last_cleared_submit_id FROM users WHERE id=?',
                           (user_id,)).fetchone()
    return int(row['last_cleared_submit_id'] or 0) if row else 0


def list_submit_attempts(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """최근 제출 시도 — 비우기 지점 이후만, 최신순. 모바일 대시보드용."""
    init()
    cleared = get_last_cleared_submit_id(user_id)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT id, round_num, idx, code, submitted, submit_status, '
            'pass_count, fail_count, ts FROM submit_attempts '
            'WHERE user_id=? AND id>? ORDER BY id DESC LIMIT ?',
            (user_id, cleared, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Fernet 키 관리 + 암호화
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_fernet() -> None:
    global _FERNET
    if _FERNET is not None:
        return
    key = os.environ.get('HYFE_IQC_FERNET_KEY', '').strip().encode('utf-8') or None
    if not key:
        os.makedirs(os.path.dirname(SECRET_KEY_PATH), exist_ok=True)
        if os.path.exists(SECRET_KEY_PATH):
            with open(SECRET_KEY_PATH, 'rb') as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key()
            with open(SECRET_KEY_PATH, 'wb') as f:
                f.write(key)
            os.chmod(SECRET_KEY_PATH, 0o600)
    _FERNET = Fernet(key)


def encrypt(text: str) -> str:
    _ensure_fernet()
    return _FERNET.encrypt(text.encode('utf-8')).decode('ascii')


_DECRYPT_LOG = logging.getLogger('genomicwqb.db.decrypt')


def decrypt(token: str) -> str:
    """Fernet 복호화. 토큰이 비어있거나 키가 바뀐 경우 빈 문자열을 돌려준다.

    실패 사유는 로그에 한 번만 남긴다 — 호출자가 빈 문자열을 받았을 때 마스터 키 회전,
    DB 손상, 토큰 누락 중 어느 케이스인지 server.log 에서 진단할 수 있도록.
    """
    _ensure_fernet()
    if not token:
        return ''
    try:
        return _FERNET.decrypt(token.encode('ascii')).decode('utf-8')
    except InvalidToken:
        _DECRYPT_LOG.warning('Fernet decrypt InvalidToken — '
                             '마스터 키 변경 또는 DB 토큰 손상 가능 (token prefix=%r)',
                             token[:12])
        return ''
    except ValueError as e:
        _DECRYPT_LOG.warning('Fernet decrypt ValueError: %s (token prefix=%r)',
                             e, token[:12])
        return ''


def code_hash(code: str) -> str:
    if not code:
        return ''
    return hashlib.sha256(SPACE_RX.sub('', code).encode('utf-8')).hexdigest()[:32]


# ─────────────────────────────────────────────────────────────────────────────
# users
# ─────────────────────────────────────────────────────────────────────────────

def upsert_user(wqb_username: str, wqb_password: str, gemini_api_key: str,
                account_type: str = 'standard') -> int:
    """로그인 검증 통과 시 호출. 기존 user 면 자격증명 업데이트, 없으면 신규.

    반환: user_id.
    """
    init()
    now = time.time()
    pw_enc = encrypt(wqb_password)
    key_enc = encrypt(gemini_api_key)
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT id FROM users WHERE wqb_username=?',
                           (wqb_username,)).fetchone()
        if row:
            uid = int(row['id'])
            conn.execute(
                'UPDATE users SET wqb_password_enc=?, gemini_api_key_enc=?, '
                'last_login_at=?, last_validated_at=?, account_type=? WHERE id=?',
                (pw_enc, key_enc, now, now, account_type, uid),
            )
            return uid
        # RC 는 정의상 API 능력이 증명된 역할이므로 backend 를 미리 'api' 로 둔다
        # (마이그레이션 백필과 일관 — 워커가 굳이 재탐침하지 않는다). standard 는
        # ''(미탐침)으로 두고 로그인/워커 첫 기동의 능력 탐침이 채운다.
        _backend = 'api' if account_type == 'research_consultant' else ''
        cur = conn.execute(
            'INSERT INTO users (wqb_username, wqb_password_enc, gemini_api_key_enc, '
            'account_type, backend, created_at, last_login_at, last_validated_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (wqb_username, pw_enc, key_enc, account_type, _backend, now, now, now),
        )
        return int(cur.lastrowid)


@_with_conn
def get_user_id_by_username(conn, wqb_username: str) -> 'int | None':
    row = conn.execute('SELECT id FROM users WHERE wqb_username=?', (wqb_username,)).fetchone()
    return row['id'] if row else None


@_with_conn
def get_account_type(conn, user_id: int) -> str:
    row = conn.execute('SELECT account_type FROM users WHERE id=?', (user_id,)).fetchone()
    return (row['account_type'] if row and row['account_type'] else 'standard')


@_with_conn
def set_account_type(conn, user_id: int, account_type: str) -> None:
    conn.execute('UPDATE users SET account_type=? WHERE id=?', (account_type, user_id))


def get_user(user_id: int) -> dict[str, Any] | None:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['wqb_password'] = decrypt(d['wqb_password_enc'])
    d['gemini_api_key'] = decrypt(d['gemini_api_key_enc'])
    return d


def find_user_by_username(wqb_username: str) -> dict[str, Any] | None:
    """username 기준 user 조회 (자격증명 복호화 포함). 없으면 None."""
    if not wqb_username:
        return None
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT * FROM users WHERE wqb_username=?',
                           (wqb_username,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['wqb_password'] = decrypt(d['wqb_password_enc'])
    d['gemini_api_key'] = decrypt(d['gemini_api_key_enc'])
    return d


def update_user_secrets(user_id: int, *, wqb_password: str | None = None,
                        gemini_api_key: str | None = None) -> None:
    """변경된 자격증명만 갱신. last_login_at 도 같이 touch."""
    init()
    now = time.time()
    sets = ['last_login_at=?']
    args: list[Any] = [now]
    if wqb_password is not None:
        sets.append('wqb_password_enc=?')
        args.append(encrypt(wqb_password))
    if gemini_api_key is not None:
        sets.append('gemini_api_key_enc=?')
        args.append(encrypt(gemini_api_key))
    args.append(user_id)
    with _DB_LOCK, _connect() as conn:
        conn.execute(f'UPDATE users SET {", ".join(sets)} WHERE id=?', tuple(args))


@_with_conn
def reset_user_running_flags(conn, user_id: int) -> None:
    """running/paused 플래그 모두 0 으로. 재시작 직후 stale 한 in-process 워커 정리용."""
    conn.execute('UPDATE users SET running=0, paused=0 WHERE id=?', (user_id,))


def delete_user_sessions(user_id: int) -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    return cur.rowcount or 0


def list_running_user_ids() -> list[int]:
    """running=1 (paused 아닌) 사용자 id 목록 — 서버 boot 시 워커 자동 재개용."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT id FROM users WHERE running=1 AND COALESCE(paused, 0)=0'
        ).fetchall()
    return [int(r['id']) for r in rows]


def list_users() -> list[dict[str, Any]]:
    """전 사용자의 (id, account_type, backend, running, paused). 세션 keeper 가
    쓴다 — 워커가 안 돌고 있어도 API 세션은 살려둬야 하니까."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT id, wqb_username, account_type, backend, running, paused FROM users'
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_credentials(user_id: int) -> tuple[str, str, str] | None:
    """(wqb_username, wqb_password, gemini_api_key) — 워커가 사용."""
    u = get_user(user_id)
    if not u:
        return None
    return u['wqb_username'], u['wqb_password'], u['gemini_api_key']


@_with_conn
def set_user_running(conn, user_id: int, running: bool, paused: bool | None = None) -> None:
    if paused is None:
        conn.execute('UPDATE users SET running=? WHERE id=?',
                     (1 if running else 0, user_id))
    else:
        conn.execute('UPDATE users SET running=?, paused=? WHERE id=?',
                     (1 if running else 0, 1 if paused else 0, user_id))


def get_user_status(user_id: int) -> dict[str, Any]:
    init()
    with _DB_LOCK, _connect() as conn:
        u = conn.execute('SELECT running, paused, last_round_num FROM users WHERE id=?',
                         (user_id,)).fetchone()
        if not u:
            return {'running': False, 'paused': False, 'last_round_num': 0,
                    'current_round': None, 'current_phase': 0,
                    'current_round_label': '—', 'current_status': 'idle'}
        last_round_num = int(u['last_round_num'] or 0)
        # 진행 중 라운드 조회 — phase 포함.
        r = conn.execute(
            'SELECT round_num, status, phase, parent_idx FROM rounds '
            'WHERE user_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1',
            (user_id,),
        ).fetchone()
    cur_round = int(r['round_num']) if r else None
    cur_phase = int(r['phase']) if r else 0
    cur_parent_idx = int(r['parent_idx']) if r and r['parent_idx'] else 0
    if cur_round is None:
        cur_label = '—'
    elif cur_phase > 0 and cur_parent_idx > 0:
        cur_label = f'{cur_round}-{cur_parent_idx}-{cur_phase}'
    elif cur_phase > 0:
        cur_label = f'{cur_round}-{cur_phase}'
    else:
        cur_label = str(cur_round)
    return {
        'running': bool(u['running']),
        'paused': bool(u['paused']),
        'last_round_num': last_round_num,
        'current_round': cur_round,
        'current_phase': cur_phase,
        'current_round_label': cur_label,
        'current_status': r['status'] if r else 'idle',
    }


# ─────────────────────────────────────────────────────────────────────────────
# sessions
# ─────────────────────────────────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    init()
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            'INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)',
            (token, user_id, now, now + SESSION_TTL_SEC),
        )
    return token


def lookup_session(token: str) -> int | None:
    """반환: user_id 또는 None (유효하지 않거나 만료)."""
    if not token:
        return None
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT user_id, expires_at FROM sessions WHERE token=?',
                           (token,)).fetchone()
        if not row:
            return None
        if float(row['expires_at']) < time.time():
            conn.execute('DELETE FROM sessions WHERE token=?', (token,))
            return None
    return int(row['user_id'])


@_with_conn
def delete_session(conn, token: str) -> None:
    conn.execute('DELETE FROM sessions WHERE token=?', (token,))


@_with_conn
def gc_sessions(conn) -> int:
    cur = conn.execute('DELETE FROM sessions WHERE expires_at<?', (time.time(),))
    return cur.rowcount or 0


# ─────────────────────────────────────────────────────────────────────────────
# rounds + alphas
# ─────────────────────────────────────────────────────────────────────────────

def start_round(user_id: int, round_num: int, *, phase: int = 0,
                 parent_idx: int = 0, focus_fail: str = '') -> int:
    """phase=0: 메인 라운드. phase>=1: focused sub-round (round_num 은 부모 메인의 번호와 동일).
    parent_idx: focused 일 때 부모 라운드의 어느 idx 알파를 개선 대상으로 삼는지.
    focus_fail: 개선해야 할 실패 테스트 설명.
    """
    init()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            'INSERT INTO rounds (user_id, round_num, status, started_at, '
            'phase, parent_idx, focus_fail) VALUES (?,?,?,?,?,?,?)',
            (user_id, round_num, 'generating', now,
             int(phase), int(parent_idx), str(focus_fail or '')),
        )
        return int(cur.lastrowid)


def get_focus_queue(user_id: int) -> list[dict[str, Any]]:
    """현재 처리 대기 중인 focused sub-round 큐. 메인 라운드 종료 시 PASS=6 알파마다 항목 추가됨."""
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT focus_queue FROM users WHERE id=?',
                           (user_id,)).fetchone()
    if not row:
        return []
    try:
        return list(json.loads(row['focus_queue'] or '[]'))
    except (json.JSONDecodeError, TypeError):
        return []


@_with_conn
def set_focus_queue(conn, user_id: int, queue: list[dict[str, Any]]) -> None:
    conn.execute('UPDATE users SET focus_queue=? WHERE id=?',
                 (json.dumps(queue or [], ensure_ascii=False), user_id))


@_with_conn
def update_round_status(conn, round_id: int, status: str) -> None:
    conn.execute('UPDATE rounds SET status=? WHERE id=?', (status, round_id))


@_with_conn
def interrupt_open_rounds(conn, summary: str = '서버 재시작으로 미완료 라운드 정리') -> int:
    now = time.time()
    cur = conn.execute(
        'UPDATE rounds SET status=?, ended_at=?, summary=? WHERE ended_at IS NULL',
        ('interrupted', now, summary),
    )
    return cur.rowcount or 0


def update_round_config(round_id: int, **fields) -> None:
    """Write bandit / generation config-snapshot columns to a rounds row.

    Only the keys provided are SET; absent keys are left unchanged.
    Tolerated column names (Phase 1 rounds schema): delay_mode, gen_temperature,
    explore_exploit, injected_arms.  Unknown keys are silently dropped so callers
    do not need to guard against schema drift.
    """
    _ALLOWED = frozenset({'delay_mode', 'gen_temperature', 'explore_exploit', 'injected_arms'})
    to_set = {k: v for k, v in fields.items() if k in _ALLOWED}
    if not to_set:
        return
    init()
    set_clause = ', '.join(f'{k}=?' for k in to_set)
    values = list(to_set.values()) + [round_id]
    try:
        with _DB_LOCK, _connect() as conn:
            conn.execute(f'UPDATE rounds SET {set_clause} WHERE id=?', values)
    except Exception:
        pass  # tolerate missing columns on older schemas


def finish_round(round_id: int, user_id: int, round_num: int, *,
                  status: str, pass_count: int, err_count: int,
                  cache_hits: int, summary: str) -> None:
    init()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            'UPDATE rounds SET status=?, ended_at=?, pass_count=?, err_count=?, '
            'cache_hits=?, summary=? WHERE id=?',
            (status, now, pass_count, err_count, cache_hits, summary, round_id),
        )
        if status == 'done':
            conn.execute('UPDATE users SET last_round_num=? WHERE id=? AND last_round_num<?',
                         (round_num, user_id, round_num))


def insert_alpha(user_id: int, round_id: int, round_num: int, alpha: dict[str, Any]) -> int:
    """알파 1행 저장. 반환값 = 새 alphas.id — focus 큐가 parent_alpha_id 로 실어
    보내 부모→자식 귀속 엣지를 잇는 데 쓴다."""
    init()
    code = alpha.get('code', '')
    # is_status 가 들어 있으면 그쪽 권위 — pass_count/fail_count/items + error/pending 갯수도 거기서 derive.
    ist = alpha.get('is_status') or {}
    p_list = list(ist.get('pass') or [])
    f_list = list(ist.get('fail') or [])
    e_list = list(ist.get('error') or [])
    pn_list = list(ist.get('pending') or [])
    if p_list or f_list or e_list or pn_list:
        pass_count = len(p_list)
        fail_count = len(f_list)
        error_count = len(e_list)
        pending_count = len(pn_list)
        pass_items = [(e.get('name') or '?').strip() for e in p_list]
        fail_items = [(e.get('name') or '?').strip() for e in f_list]
    else:
        pass_count = int(alpha.get('pass_count') or 0)
        fail_count = int(alpha.get('fail_count') or 0)
        error_count = int(alpha.get('error_count') or 0)
        pending_count = int(alpha.get('pending_count') or 0)
        pass_items = list(alpha.get('pass_items') or [])
        fail_items = list(alpha.get('fail_items') or [])
    if 'settings' in alpha or 'delay' in alpha:
        eff = _settings_fp.effective_settings(alpha.get('settings') or {}, alpha.get('delay', ''))
        fp = _settings_fp.settings_fingerprint(eff)
    else:
        eff, fp = {}, None
    # self_corr: 최상위 키 우선, 없으면 metrics 안에서 폴백
    metrics = dict(alpha.get('metrics') or {})
    _sc = alpha.get('self_corr')
    if _sc is None:
        _sc = metrics.get('self_corr')
    # 지표 컬럼 (sharpe/fitness/turnover/drawdown/margin/returns)
    _sharpe   = _coerce_float_or_none(metrics.get('sharpe'))
    _fitness  = _coerce_float_or_none(metrics.get('fitness'))
    _turnover = _coerce_float_or_none(metrics.get('turnover'))
    _drawdown = _coerce_float_or_none(metrics.get('drawdown'))
    _margin   = _coerce_float_or_none(metrics.get('margin'))
    _returns  = _coerce_float_or_none(metrics.get('returns'))
    # lineage
    _generation      = int(alpha.get('generation') or 0)
    _parent_alpha_id = alpha.get('parent_alpha_id')
    # 유전체 원본 — 있으면 그대로 보존한다. 없으면(레거시/Gemini 경로) NULL.
    _genome_obj = alpha.get('genome')
    _genome = (json.dumps(dict(_genome_obj), ensure_ascii=False)
               if isinstance(_genome_obj, dict) and _genome_obj else None)
    # 변이 귀속 (v6) — 빈 값은 NULL 로 저장해 directive_stats 필터를 단순하게.
    _origin = (str(alpha.get('origin') or '').strip() or None)
    _directive = (str(alpha.get('directive') or '').strip() or None)
    _gc = alpha.get('genes_changed')
    _genes_changed = (json.dumps(list(_gc), ensure_ascii=False)
                      if isinstance(_gc, (list, tuple)) else None)
    _spec_id = alpha.get('spec_id')

    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            'INSERT INTO alphas (user_id, round_id, round_num, idx, code, code_hash, desc, '
            'pass_count, pass_items, fail_count, fail_items, error_text, metrics, mode, '
            'cached, submitted, submit_status, error_count, pending_count, phase, ts, '
            'region, universe, delay, neutralization, decay, truncation, settings_fp, self_corr, '
            'sharpe, fitness, turnover, drawdown, margin, returns, generation, parent_alpha_id, '
            'genome, origin, directive, genes_changed, spec_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                user_id, round_id, round_num, int(alpha.get('idx') or 0),
                code, code_hash(code), alpha.get('desc', ''),
                pass_count,
                json.dumps(pass_items, ensure_ascii=False),
                fail_count,
                json.dumps(fail_items, ensure_ascii=False),
                alpha.get('error_text', ''),
                json.dumps(dict(alpha.get('metrics') or {}), ensure_ascii=False),
                alpha.get('mode', ''),
                1 if alpha.get('cached') else 0,
                # 제출 여부는 단일 규칙으로 정규화 — 호출부가 무엇을 넘기든
                # '구체 수치 동반 self-corr 거절' 이 아니면 제출로 기록.
                1 if effectively_submitted(alpha.get('submitted'),
                                           str(alpha.get('submit_status') or '')) else 0,
                str(alpha.get('submit_status') or ''),
                error_count,
                pending_count,
                int(alpha.get('phase') or 0),
                time.time(),
                eff.get('region'), eff.get('universe'),
                int(eff['delay']) if str(eff.get('delay', '')).lstrip('-').isdigit() else None,
                eff.get('neutralization'),
                int(float(eff['decay'])) if eff.get('decay') not in (None, '') else None,
                float(eff['truncation']) if eff.get('truncation') not in (None, '') else None,
                fp,
                _coerce_float_or_none(_sc),
                _sharpe, _fitness, _turnover, _drawdown, _margin, _returns,
                _generation, _parent_alpha_id, _genome,
                _origin, _directive, _genes_changed,
                int(_spec_id) if _spec_id is not None else None,
            ),
        )
        return int(cur.lastrowid)


def lookup_alpha_by_hash(user_id: int, h: str,
                         settings_fp: str | None = None) -> dict[str, Any] | None:
    """동일 알파 코드의 가장 최근 시뮬 결과 — 사용자 전체에서 검색 (cross-user).

    settings_fp 가 주어지면 같은 settings 로 시뮬된 행만 매칭한다 (정확성). None 이면
    기존 동작(코드만). user_id 는 호환을 위해 받지만 필터에 쓰지 않는다.
    cross-user 공유는 의도된 효율 설계 — 단, WQB 티어별 데이터 접근 차이로 동일
    code+settings 가 사용자별 다른 결과를 낼 수 있다(알려진 한계).
    """
    if not h:
        return None
    init()
    with _DB_LOCK, _connect() as conn:
        if settings_fp is not None:
            row = conn.execute(
                'SELECT * FROM alphas WHERE code_hash=? AND settings_fp=? '
                'ORDER BY id DESC LIMIT 1',
                (h, settings_fp),
            ).fetchone()
        else:
            row = conn.execute(
                'SELECT * FROM alphas WHERE code_hash=? ORDER BY id DESC LIMIT 1',
                (h,),
            ).fetchone()
    return _alpha_view(row) if row else None


def list_recent_distinct_codes(limit: int = 80) -> list[str]:
    """가장 최근 시뮬한 distinct 알파 코드 — 사용자 전체에서. Gemini 회피 가이드용
    (이 코드들은 이미 캐시에 있어 다시 만들면 cache hit 으로 무시됨)."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT code FROM alphas WHERE id IN ('
            '  SELECT MAX(id) FROM alphas GROUP BY code_hash'
            ') ORDER BY id DESC LIMIT ?',
            (int(limit),),
        ).fetchall()
    return [r['code'] for r in rows if r['code']]


def list_recent_alphas(user_id: int, limit: int = 60) -> list[dict[str, Any]]:
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM alphas WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (user_id, limit),
        ).fetchall()
    return [_alpha_view(r) for r in rows]


def list_submitted_alphas(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """WQB Submit 클릭이 발생한 모든 알파 — 성공 / 거절 둘 다 포함, 최신순.

    UI 가 Submitted vs Unsubmitted 를 구분해 표시.
    submit_status='submitted' → Submitted, 'rejected:*' → Unsubmitted (거절됨).
    """
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alphas WHERE user_id=? AND "
            "(submitted=1 OR submit_status LIKE 'rejected:%') "
            'ORDER BY id DESC LIMIT ?',
            (user_id, limit),
        ).fetchall()
    return [_alpha_view(r) for r in rows]


def list_rejected_alpha_codes(user_id: int, limit: int = 40) -> list[str]:
    """Submit 가 거절됐거나(self-correlation, "Cannot submit" 등) 응답 자체를 못 받은
    (no_response_modal_less 등) 알파 코드 — 최신순, dedup.

    list_submitted_alphas() 는 submit_status LIKE 'rejected:%' 만 잡고 'fail:%'
    (no_response 등) 는 놓치므로 그쪽 보강용. submitted=1 (실제 제출 성공) 은 제외 —
    그건 list_submitted_alphas 담당. 다음 라운드 사전 유사도 검사 + Gemini 회피 가이드에
    써서, 같은 영역을 다시 생성해도 또 거절되는 낭비를 막는다.
    """
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT code FROM alphas WHERE user_id=? AND submitted=0 AND "
            "(submit_status LIKE 'rejected:%' OR submit_status LIKE 'fail:%') "
            'ORDER BY id DESC LIMIT ?',
            (user_id, int(limit)),
        ).fetchall()
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        c = (r['code'] or '').strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


ELITE_WINDOW = int(os.environ.get('IQC_ELITE_WINDOW', '400'))
"""엘리트를 고르는 최근성 윈도우(알파 개수). 과거 전체를 보면 풀이 '화석'에서 굳는다."""

ELITE_MIN_SCORE = float(os.environ.get('IQC_ELITE_MIN_SCORE', '0.02'))
"""시드가 되기 위한 selection_score 하한. 0 = 시뮬 실패/무지표 알파(부모 자격 없음)."""

HALL_OF_FAME_N = int(os.environ.get('IQC_HALL_OF_FAME_N', '2'))
"""최근성 윈도우 **밖**의 역대 최고 유전체에 예약하는 시드 슬롯 수. 0 = 끄기.

왜 필요한가 (2026-07-14): ELITE_WINDOW=400 은 화석화를 막지만, 그 대가로 **역대 최고
알파가 400개 뒤로 밀리는 순간 유전자 풀에서 영구 소멸**한다. 라이브에서 6월의 Sharpe
3.77 / 3.43 알파(레짐 조건부 + hump + 서브인더스트리 중립화)가 정확히 그렇게 사라졌고,
7/12 콜드스타트 이후 풀의 최고가 1.42 에 머물렀다. 윈도우는 유지하되, 역대 최고 K개를
별도 슬롯으로 되돌려 그 유전자가 교차 재료로 계속 살아 있게 한다.
소수 슬롯(기본 2/5)으로 제한해 화석이 풀을 점거하지는 못하게 한다.
"""

HALL_OF_FAME_POOL = int(os.environ.get('IQC_HALL_OF_FAME_POOL', '60'))
"""명예의 전당 후보를 sharpe 상위 몇 행에서 고를지 (그중 selection_score 로 재정렬)."""


def _hydrate_alpha_row(r) -> dict[str, Any] | None:
    """alphas 행 → 시드 dict(metrics/genome 파싱 + _sharpe/_score 계산). 부적격이면 None."""
    from . import reward as _reward
    d = dict(r)
    try:
        d['metrics'] = json.loads(d.get('metrics') or '{}')
    except Exception:
        d['metrics'] = {}
    try:
        d['genome'] = json.loads(d.get('genome') or '{}')
    except Exception:
        return None
    if not isinstance(d['genome'], dict) or not d['genome']:
        return None
    # 유전체 JSON 이 lineage 의 권위. 컬럼은 폴백.
    d['genome'].setdefault('generation', int(d.get('generation') or 0))
    sh = d['metrics'].get('sharpe')
    try:
        d['_sharpe'] = float(str(sh).strip()) if sh not in (None, '') else 0.0
    except (ValueError, TypeError):
        d['_sharpe'] = 0.0
    d['_score'] = _reward.selection_score(
        d['metrics'],
        pass_count=int(d.get('pass_count') or 0),
        fail_count=int(d.get('fail_count') or 0),
        error_count=int(d.get('error_count') or 0),
        self_corr=d.get('self_corr'),
    )
    return d


_SEED_COLS = ('id, code, code_hash, desc, pass_count, fail_count, error_count, '
              'metrics, round_num, idx, universe, neutralization, decay, truncation, '
              'self_corr, generation, genome')


def hall_of_fame_seeds(user_id: int, top_n: int = 2, *,
                       pool: int | None = None) -> list[dict[str, Any]]:
    """역대(윈도우 무관) 최고 유전체 top_n 개. 유전체가 없는 행은 애초에 후보가 아니다.

    sharpe 상위 `pool` 행을 먼저 뽑고(인덱스 친화적), 그 안에서 selection_score 로 재정렬한다.
    sharpe 만으로 고르면 turnover/self-corr 가 망가진 알파가 올라온다.
    """
    if top_n <= 0:
        return []
    init()
    _pool = int(pool if pool is not None else HALL_OF_FAME_POOL)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f'SELECT {_SEED_COLS} FROM alphas '
            'WHERE user_id=? AND genome IS NOT NULL '
            "AND TRIM(error_text) = '' AND sharpe IS NOT NULL "
            'ORDER BY sharpe DESC LIMIT ?',
            (user_id, _pool),
        ).fetchall()
    best_by_code: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _hydrate_alpha_row(r)
        if d is None:
            continue
        key = d.get('code_hash') or d.get('code') or ''
        prev = best_by_code.get(key)
        if prev is None or d['_score'] > prev['_score']:
            best_by_code[key] = d
    out = sorted(best_by_code.values(),
                 key=lambda d: (d['_score'], d['id']), reverse=True)
    return out[:top_n]


def elite_seeds(user_id: int, top_n: int = 5, *,
                window: int | None = None,
                min_score: float | None = None,
                hall_of_fame: int | None = None) -> list[dict[str, Any]]:
    """다음 라운드의 교차/변이 재료가 될 엘리트 유전체 top_n 개.

    구(舊) `best_alphas_for_seeding` 을 대체한다. 그 함수는 세 가지가 동시에 틀렸다
    (2026-07-11 라이브 진단, uid2 round 66~250):

    1. `WHERE pass_count >= 5` 하드 게이트. pass_count 는 최대 ~7 인 이산 카운터라
       자식(최대 4)이 절대 통과하지 못한다 → 풀이 184 라운드째 동결.
    2. 최근성 윈도우 없음 + `ORDER BY pass_count DESC, id DESC` → 언제나 **같은 5행**.
    3. 유전체를 코드에서 정규식으로 역추출 → 부모를 복제조차 못 하는 자식 생산.

    새 규칙: **유전체를 실제로 보유한**(genome IS NOT NULL) 최근 `window` 개 알파를
    후보로 삼고, 연속 적합도 `reward.selection_score` 상위 top_n 을 고른다. 동점이면
    최신 행이 이긴다. 같은 코드는 최고점 1건만 남긴다(풀이 자기복제로 붕괴하는 것을 막는다).

    각 dict 은 `genome`(dict, 정확한 `generation` 포함) 과 `_score` 를 갖는다.
    """
    init()
    _window = int(window if window is not None else ELITE_WINDOW)
    _floor = float(min_score if min_score is not None else ELITE_MIN_SCORE)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f'SELECT {_SEED_COLS} '
            'FROM alphas WHERE user_id=? AND genome IS NOT NULL '
            "AND TRIM(error_text) = '' "
            'ORDER BY id DESC LIMIT ?',
            (user_id, _window),
        ).fetchall()

    best_by_code: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _hydrate_alpha_row(r)
        if d is None or d['_score'] < _floor:
            continue
        # dedup 키 = code_hash (코드가 같으면 한 자리만). 유전체 '형태' 기준 추가 dedup 은
        # 일부러 안 한다 — render() 는 유전체의 순함수라 형태가 같으면 코드도 같아서
        # 여기서 이미 걸리고, 형태만 보고 합치면 유전체가 빈약한 행들이 통째로 한 개로
        # 뭉개진다(2026-07-14 시도 → 시드 풀이 1개로 붕괴해 되돌림).
        # 다양성은 아래 NSGA-II 선택층(crowding)이 담당한다.
        key = d.get('code_hash') or d.get('code') or ''
        prev = best_by_code.get(key)
        if prev is None or d['_score'] > prev['_score']:
            best_by_code[key] = d

    # 동점이면 최신(id 큰) 쪽이 이긴다 — 적합도가 평평한 구간에서 풀이 옛 행에 눌러앉지
    # 않게 하는 anti-fossil 타이브레이크. 암묵적 안정정렬에 기대지 않고 명시한다.
    out = sorted(best_by_code.values(),
                 key=lambda d: (d['_score'], d['id']), reverse=True)

    # 선택층 — IQC_SELECTION_MODE 로 score↔percentile↔NSGA-II 전환(즉시 롤백 가능).
    #   percentile 은 IQC_SELECTION_DIVERSITY_LAM>0 이면 코드 Jaccard fitness-sharing 적용.
    #   실패/미지정은 위의 selection_score 내림차순으로 안전 폴백.
    #
    # 2026-07-14: 기본을 'ref' → 'nsga2' 로 전환한다. 단일 가중합(ref)은 다양성 압력이
    # 없어 엘리트 풀이 '균형점 화석' 하나로 수렴한다. NSGA-II 는 (sharpe, fitness,
    # -turnover, -self_corr, 2Y sharpe) 파레토 면을 유지하므로 '고 Sharpe·저 Fitness'
    # 같은 극단 개체가 살아남아 교차 재료가 된다. 롤백: IQC_SELECTION_MODE=ref.
    _mode = os.environ.get('IQC_SELECTION_MODE', 'nsga2')
    if _mode in ('percentile', 'nsga2') and out:
        try:
            from . import selection
            _lam = float(os.environ.get('IQC_SELECTION_DIVERSITY_LAM', '0') or 0)
            _sim = None
            if _lam > 0:
                from . import alpha_similarity
                _sim = alpha_similarity.similarity
            _order = selection.order_seed_records(out, mode=_mode, lam=_lam, sim_fn=_sim)
            if _order is not None:
                out = [out[i] for i in _order]
        except Exception as e:
            logging.getLogger('genomicwqb.db').warning(
                'selection mode=%s 실패, score 폴백: %s', _mode, e)

    # ── 명예의 전당 슬롯 ────────────────────────────────────────────────────────
    # 최근성 윈도우 **밖**으로 밀려난 역대 최고 유전체를 소수 슬롯만큼 되돌린다.
    # 없으면 6월의 Sharpe 3.77 같은 유전자가 풀에서 영구 소멸한다(HALL_OF_FAME_N 참조).
    # ⚠ 이것은 의도적으로 `window` 계약을 넘어선다 — 그게 존재 이유다. 순수한 윈도우
    #    의미론이 필요한 호출부(테스트 포함)는 hall_of_fame=0 을 넘겨 끌 수 있다.
    # 윈도우 후보를 밀어내지 않도록 top_n 의 절반까지만 내준다.
    _hof_n = HALL_OF_FAME_N if hall_of_fame is None else int(hall_of_fame)
    hof_slots = min(_hof_n, max(0, top_n // 2))
    if hof_slots > 0:
        try:
            seen_codes = {d.get('code_hash') or d.get('code') or ''
                          for d in out[:top_n]}
            hof = [d for d in hall_of_fame_seeds(user_id, top_n=hof_slots + 3)
                   if (d.get('code_hash') or d.get('code') or '') not in seen_codes]
            if hof:
                keep = out[:max(0, top_n - hof_slots)]
                out = keep + hof[:hof_slots]
        except Exception as e:
            logging.getLogger('genomicwqb.db').warning('hall-of-fame 시드 실패(무시): %s', e)
    return out[:top_n]


def operator_preference_stats(user_id: int, lookback_alphas: int = 200,
                                top_n_ops: int = 8,
                                top_n_fields: int = 12) -> dict[str, Any]:
    """smilee 의 ops_picking_prob 정신: 최근 N 알파 중 PASS 가 잘 나온 것에 자주 쓰인
    operator/datafield 를 통계로 뽑아 다음 라운드 prompt 에 'preference' 로 넣음.

    반환: {'top_ops': [(name, avg_pass_count, n_used)], 'top_fields': 동일 형태}
    """
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT code, pass_count FROM alphas WHERE user_id=? '
            'ORDER BY id DESC LIMIT ?',
            (user_id, lookback_alphas),
        ).fetchall()
    if not rows:
        return {'top_ops': [], 'top_fields': []}

    # 알파 코드에서 operator name + datafield identifier 추출.
    op_pat = re.compile(r'\b([a-z_][a-z0-9_]*)\s*\(', re.IGNORECASE)
    tok_pat = re.compile(r'\b([a-z][a-z0-9_]{2,})\b', re.IGNORECASE)
    op_used: dict[str, list[int]] = {}   # name -> [pass_count_list]
    field_used: dict[str, list[int]] = {}
    for r in rows:
        code = r['code'] or ''
        pc = int(r['pass_count'] or 0)
        ops_in_code = {m.group(1).lower() for m in op_pat.finditer(code)
                       if _operator_catalog.is_operator(m.group(1))}
        for op in ops_in_code:
            op_used.setdefault(op, []).append(pc)
        for m in tok_pat.finditer(code):
            tok = m.group(1).lower()
            if _operator_catalog.is_operator(tok) or len(tok) < 4:
                continue
            field_used.setdefault(tok, []).append(pc)

    def _rank(d: dict[str, list[int]], top_n: int):
        # 평균 pass_count 가 높고 사용 횟수도 많은 것 우선.
        scored = [(name, sum(lst) / len(lst), len(lst))
                  for name, lst in d.items() if len(lst) >= 3]
        # avg_pass * sqrt(n_used) 로 점수 매겨 너무 적게 쓰인 것에 over-fit 방지.
        scored.sort(key=lambda t: t[1] * (t[2] ** 0.5), reverse=True)
        return [(n, round(a, 2), c) for (n, a, c) in scored[:top_n]]

    return {
        'top_ops': _rank(op_used, top_n_ops),
        'top_fields': _rank(field_used, top_n_fields),
    }


def list_recent_alpha_summaries(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """모바일용 — 최근 N개 알파의 PASS/FAIL/ERROR/PENDING 카운트 + submit 상태.
    캐시 hit 알파는 제외 (cached=1) — 사용자가 보고 싶은 건 새로 시뮬한 알파.
    phase>0 인 focused sub-round 알파는 round_label 을 'N-M' 형태로 함께 반환."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT round_num, idx, pass_count, fail_count, error_count, pending_count, '
            'submitted, submit_status, error_text, code, phase, ts '
            'FROM alphas WHERE user_id=? AND cached=0 ORDER BY id DESC LIMIT ?',
            (user_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        ph = int(d.get('phase') or 0)
        d['round_label'] = f"{int(d['round_num'])}-{ph}" if ph > 0 else str(int(d['round_num']))
        out.append(d)
    return out


def submitted_count(user_id: int) -> int:
    """별표 '저장' 성공 시도 수 — submitted=1 인 시도만 (별표 클릭 성공).
    비우기 지점(last_cleared_submit_id) 이후 submit_attempts 만 집계하므로
    "화면 비우기" 시 함께 0 으로 리셋된다.
    (별표 정책: all-pass & self-corr<=0.7 인 후보만 시도로 기록되며, 그중
    별표 클릭 성공 시에만 submitted=1. corr>0.7·별표실패·구버전 거절은 submitted=0.)"""
    init()
    cleared = get_last_cleared_submit_id(user_id)
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM submit_attempts '
            'WHERE user_id=? AND id>? AND submitted=1',
            (user_id, cleared),
        ).fetchone()
    return int(row['n'] or 0)


def unsubmitted_count(user_id: int) -> int:
    """별표 '미저장' 시도 수 — 시도됐으나 submitted=0 (self-corr>0.7, 별표 실패,
    또는 구버전 self-corr 거절). submitted_count 과 동일하게 비우기 지점 이후만
    집계 → "화면 비우기" 시 함께 0 으로 리셋된다."""
    init()
    cleared = get_last_cleared_submit_id(user_id)
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM submit_attempts '
            'WHERE user_id=? AND id>? AND submitted=0',
            (user_id, cleared),
        ).fetchone()
    return int(row['n'] or 0)


def _alpha_view(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ('pass_items', 'fail_items', 'metrics'):
        try:
            d[k] = json.loads(d.get(k) or ('[]' if k != 'metrics' else '{}'))
        except Exception:
            d[k] = [] if k != 'metrics' else {}
    d['cached'] = bool(d.get('cached'))
    d['round'] = d.get('round_num', 0)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# errors
# ─────────────────────────────────────────────────────────────────────────────

def upsert_error(user_id: int, round_num: int, code: str, error_text: str) -> bool:
    """오류 정규화 + dedupe. 신규 패턴이면 True."""
    init()
    err_msg = (error_text or '')[:300]
    if not err_msg:
        return False
    pattern = re.sub(r'"[^"]+"', '"X"', err_msg)
    pattern = re.sub(r"'[^']+'", "'X'", pattern)
    identifiers = re.findall(r'"([^"]+)"', err_msg) + re.findall(r"'([^']+)'", err_msg)
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT id, count, identifiers FROM errors WHERE user_id=? AND pattern=?',
            (user_id, pattern),
        ).fetchone()
        if row:
            try:
                old_ids = set(json.loads(row['identifiers'] or '[]'))
            except Exception:
                old_ids = set()
            old_ids.update(identifiers)
            conn.execute(
                'UPDATE errors SET count=count+1, identifiers=?, last_round=?, last_seen=? '
                'WHERE id=?',
                (json.dumps(sorted(old_ids)[:60], ensure_ascii=False),
                 round_num, now, row['id']),
            )
            return False
        conn.execute(
            'INSERT INTO errors (user_id, pattern, count, identifiers, sample_code, '
            'first_round, last_round, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)',
            (user_id, pattern, 1,
             json.dumps(sorted(set(identifiers))[:60], ensure_ascii=False),
             (code or '')[:200], round_num, round_num, now, now),
        )
        return True


def list_error_patterns(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    """오류 패턴 — 사용자 전체에서 GROUP BY pattern 으로 누적 집계.

    user_id 인자는 호환성을 위해 받지만 필터에 쓰지 않는다. 다른 사용자가 이미 마주친
    오류 패턴을 본인의 Gemini 회피 가이드 시드로 재사용 — 모두 같은 WQB / 같은 datafield
    환경이라 패턴은 cross-user 로 유효함.
    """
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT pattern, SUM(count) AS count, '
            "GROUP_CONCAT(identifiers, '|') AS identifiers, "
            'MAX(sample_code) AS sample_code, '
            'MIN(first_round) AS first_round, MAX(last_round) AS last_round, '
            'MAX(last_seen) AS last_seen '
            'FROM errors GROUP BY pattern '
            'ORDER BY count DESC, last_seen DESC LIMIT ?',
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        # GROUP_CONCAT 결과는 '[a,b]|[c,d]' 같이 들어옴 — 합쳐서 dedup.
        merged_ids: set[str] = set()
        for chunk in (r['identifiers'] or '').split('|'):
            try:
                merged_ids.update(json.loads(chunk or '[]'))
            except Exception:
                continue
        out.append({
            'pattern': r['pattern'],
            'count': int(r['count'] or 0),
            'identifiers': sorted(merged_ids)[:60],
            'sample_code': r['sample_code'] or '',
            'first_round': int(r['first_round'] or 0),
            'last_round': int(r['last_round'] or 0),
        })
    return out


def total_errors_count(user_id: int) -> int:
    """오류 패턴 수 — 사용자 전체에서 distinct pattern 갯수 (user_id 무시)."""
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT COUNT(DISTINCT pattern) AS n FROM errors'
        ).fetchone()
    return int(row['n'] or 0)


# ─────────────────────────────────────────────────────────────────────────────
# feedback (FIFO 30개)
# ─────────────────────────────────────────────────────────────────────────────

def append_feedback(user_id: int, round_num: int, payload: dict[str, Any]) -> None:
    init()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            'INSERT INTO feedback (user_id, round_num, payload, ts) VALUES (?,?,?,?)',
            (user_id, round_num, json.dumps(payload, ensure_ascii=False), now),
        )
        # FIFO trim — FEEDBACK_CAP 개를 초과하는 가장 오래된 row 삭제.
        conn.execute(
            'DELETE FROM feedback WHERE user_id=? AND id NOT IN ('
            '    SELECT id FROM feedback WHERE user_id=? ORDER BY id DESC LIMIT ?'
            ')',
            (user_id, user_id, FEEDBACK_CAP),
        )


def list_feedback(user_id: int) -> list[dict[str, Any]]:
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT payload FROM feedback WHERE user_id=? ORDER BY id ASC',
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r['payload']))
        except Exception:
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# logs (라운드 로그 한 줄씩 저장 + SSE)
# ─────────────────────────────────────────────────────────────────────────────

def append_log(user_id: int, round_num: int, line: str, level: str = 'info') -> int:
    init()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            'INSERT INTO logs (user_id, round_num, ts, level, line) VALUES (?,?,?,?,?)',
            (user_id, round_num, now, level, line),
        )
        return int(cur.lastrowid)


def list_logs_since(user_id: int, since_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    """SSE 폴링/REST 가 사용. since_id 이후의 로그 ID 오름차순."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT id, round_num, ts, level, line FROM logs '
            'WHERE user_id=? AND id>? ORDER BY id ASC LIMIT ?',
            (user_id, int(since_id or 0), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_log_id(user_id: int) -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT MAX(id) AS m FROM logs WHERE user_id=?',
                           (user_id,)).fetchone()
    return int(row['m'] or 0)


def list_rounds(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT id, round_num, status, started_at, ended_at, pass_count, err_count, '
            'cache_hits, summary FROM rounds WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# bandit_arms — per-user 다중 armed bandit arm 보상 통계 (라운드/재시작 영속)
#
# 설계 선택:
#   - decay 는 reward_sum/reward_sq_sum 에만 적용, visits 는 원시 카운트 유지.
#     visits 를 decay 하면 UCB 탐험 항(√(ln N / n)) 계산이 불안정해지므로
#     visits 는 실제 시도 횟수를 그대로 누적한다.
#   - upsert 는 _DB_LOCK 아래 SELECT-then-INSERT/UPDATE 로 처리 — 원자성 보장.
#   - 알파 완료마다 즉시 호출 (per-update flush); 배치 없음.
# ─────────────────────────────────────────────────────────────────────────────

def bandit_update(user_id: int, arm_key: str, reward: float, round_num: int,
                  *, dimension: str = '', decay_k: float = 0.0) -> None:
    """arm 의 보상 통계를 갱신(upsert). 알파 완료마다 즉시 flush.

    decay_k>0 이면 기존 통계에 exp(-decay_k*(round_num-last_round)) 시간감쇠를 적용한 뒤
    새 reward 를 더한다 (오래된 보상의 가중치를 줄임). visits 는 감쇠하지 않는다
    (UCB 탐험 항 분모를 안정적으로 유지하기 위해 원시 카운트 보존).
    """
    r = _coerce_float_or_none(reward)
    if r is None:
        r = 0.0
    now = time.time()
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT reward_sum, reward_sq_sum, visits, last_round, dimension '
            'FROM bandit_arms WHERE user_id=? AND arm_key=?',
            (user_id, arm_key),
        ).fetchone()
        if row:
            rs = float(row['reward_sum'])
            rss = float(row['reward_sq_sum'])
            vis = int(row['visits'])
            lr = int(row['last_round'])
            new_last_round = max(lr, int(round_num))
            new_dim = dimension or row['dimension']
            if decay_k > 0:
                f = math.exp(-decay_k * max(0, round_num - lr))
                rs *= f
                rss *= f
            rs += r
            rss += r * r
            vis += 1
            conn.execute(
                'UPDATE bandit_arms SET reward_sum=?, reward_sq_sum=?, visits=?, '
                'last_round=?, updated_at=?, dimension=? WHERE user_id=? AND arm_key=?',
                (rs, rss, vis, new_last_round, now, new_dim, user_id, arm_key),
            )
        else:
            conn.execute(
                'INSERT INTO bandit_arms (user_id, arm_key, dimension, '
                'reward_sum, reward_sq_sum, visits, last_round, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (user_id, arm_key, dimension or '', r, r * r, 1, round_num, now),
            )


def bandit_stats(user_id: int, dimension: str | None = None) -> list[dict[str, Any]]:
    """arm 별 통계 리스트. 각: {arm_key, dimension, visits, mean, var, reward_sum, reward_sq_sum, last_round}.

    mean = reward_sum/visits (visits=0 → 0). var = reward_sq_sum/visits - mean^2 (clamp >=0).
    reward_sq_sum 은 호출자가 2차 모멘트를 병합할 때 사용.
    dimension 이 주어지면 해당 dimension 인 arm 만 반환.
    """
    init()
    with _DB_LOCK, _connect() as conn:
        if dimension is not None:
            rows = conn.execute(
                'SELECT arm_key, dimension, visits, reward_sum, reward_sq_sum, last_round '
                'FROM bandit_arms WHERE user_id=? AND dimension=? ORDER BY id ASC',
                (user_id, dimension),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT arm_key, dimension, visits, reward_sum, reward_sq_sum, last_round '
                'FROM bandit_arms WHERE user_id=? ORDER BY id ASC',
                (user_id,),
            ).fetchall()
    out = []
    for row in rows:
        vis = int(row['visits'])
        rs = float(row['reward_sum'])
        rss = float(row['reward_sq_sum'])
        mean = rs / vis if vis > 0 else 0.0
        var = max(0.0, rss / vis - mean * mean) if vis > 0 else 0.0
        out.append({
            'arm_key': row['arm_key'],
            'dimension': row['dimension'],
            'visits': vis,
            'mean': mean,
            'var': var,
            'reward_sum': rs,
            'reward_sq_sum': rss,
            'last_round': int(row['last_round']),
        })
    return out


def bandit_arm(user_id: int, arm_key: str) -> dict[str, Any] | None:
    """단일 arm dict 또는 None. 반환 키: arm_key, dimension, visits, mean, var, reward_sum, reward_sq_sum, last_round."""
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT arm_key, dimension, visits, reward_sum, reward_sq_sum, last_round '
            'FROM bandit_arms WHERE user_id=? AND arm_key=?',
            (user_id, arm_key),
        ).fetchone()
    if row is None:
        return None
    vis = int(row['visits'])
    rs = float(row['reward_sum'])
    rss = float(row['reward_sq_sum'])
    mean = rs / vis if vis > 0 else 0.0
    var = max(0.0, rss / vis - mean * mean) if vis > 0 else 0.0
    return {
        'arm_key': row['arm_key'],
        'dimension': row['dimension'],
        'visits': vis,
        'mean': mean,
        'var': var,
        'reward_sum': rs,
        'reward_sq_sum': rss,
        'last_round': int(row['last_round']),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Adaptive-exploration leaderboard queries (Part A)
#
# all_pass 정의: fail_count=0 AND error_count=0 AND pass_count>=7.
# NULL axis 컬럼 행은 무시 (legacy rows without settings).
# ─────────────────────────────────────────────────────────────────────────────

_AXIS_COLUMNS = frozenset({'universe', 'neutralization', 'region'})
# 'decay' 는 버킷으로 그룹화 (bandit.decay_to_bucket 재사용).


def axis_effectiveness(user_id: int, axis: str,
                        lookback_rounds: int = 20) -> list[dict[str, Any]]:
    """최근 lookback_rounds 라운드의 알파를 axis 컬럼 값별로 집계.

    axis in {'universe', 'neutralization', 'decay', 'region'}.
    'decay' 는 raw 정수 대신 버킷 ('low'|'mid'|'high') 으로 그룹화 — bandit.decay_to_bucket 기준.

    반환: [{'value':str, 'count':int, 'all_pass_rate':float,
             'avg_pass_count':float, 'avg_sharpe':float|None,
             'avg_self_corr':float|None}]
          all_pass_rate 내림차순 정렬. 빈 리스트 가능.
    NULL axis 값인 행, count<1 인 버킷은 제외.
    """
    if axis not in {'universe', 'neutralization', 'decay', 'region'}:
        return []
    init()
    # max round_num 로 lookback window 계산
    with _DB_LOCK, _connect() as conn:
        max_row = conn.execute(
            'SELECT MAX(round_num) AS m FROM alphas WHERE user_id=?',
            (user_id,),
        ).fetchone()
        max_rn = int(max_row['m'] or 0) if max_row else 0
        min_rn = max(1, max_rn - lookback_rounds + 1)

        if axis == 'decay':
            # decay 는 Python 측 버킷 그룹화
            rows = conn.execute(
                'SELECT decay, pass_count, fail_count, error_count, sharpe, self_corr '
                'FROM alphas WHERE user_id=? AND round_num>=? AND decay IS NOT NULL',
                (user_id, min_rn),
            ).fetchall()
    if axis == 'decay':
        # Python-side bucketing
        from . import bandit as _bandit
        bucket_data: dict[str, list[dict]] = {}
        for r in rows:
            bkt = _bandit.decay_to_bucket(r['decay'])
            bucket_data.setdefault(bkt, []).append(dict(r))
        out = []
        for bkt, items in bucket_data.items():
            n = len(items)
            n_all_pass = sum(
                1 for x in items
                if int(x['fail_count'] or 0) == 0
                and int(x['error_count'] or 0) == 0
                and int(x['pass_count'] or 0) >= 7
            )
            avg_pc = sum(int(x['pass_count'] or 0) for x in items) / n
            sharpes = [float(x['sharpe']) for x in items if x['sharpe'] is not None]
            corrs = [float(x['self_corr']) for x in items if x['self_corr'] is not None]
            out.append({
                'value': bkt,
                'count': n,
                'all_pass_rate': n_all_pass / n,
                'avg_pass_count': avg_pc,
                'avg_sharpe': sum(sharpes) / len(sharpes) if sharpes else None,
                'avg_self_corr': sum(corrs) / len(corrs) if corrs else None,
            })
        out.sort(key=lambda d: d['all_pass_rate'], reverse=True)
        return out

    # SQL-side grouping for string axes
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f'SELECT {axis} AS axis_val, '
            '  COUNT(*) AS n, '
            '  SUM(CASE WHEN fail_count=0 AND error_count=0 AND pass_count>=7 THEN 1 ELSE 0 END) AS n_all_pass, '
            '  AVG(pass_count) AS avg_pc, '
            '  AVG(sharpe) AS avg_sharpe, '
            '  AVG(self_corr) AS avg_self_corr '
            f'FROM alphas WHERE user_id=? AND round_num>=? AND {axis} IS NOT NULL '
            f'GROUP BY {axis}',
            (user_id, min_rn),
        ).fetchall()
    out = []
    for r in rows:
        n = int(r['n'] or 0)
        if n < 1:
            continue
        n_ap = int(r['n_all_pass'] or 0)
        out.append({
            'value': str(r['axis_val']),
            'count': n,
            'all_pass_rate': n_ap / n,
            'avg_pass_count': float(r['avg_pc'] or 0),
            'avg_sharpe': float(r['avg_sharpe']) if r['avg_sharpe'] is not None else None,
            'avg_self_corr': float(r['avg_self_corr']) if r['avg_self_corr'] is not None else None,
        })
    out.sort(key=lambda d: d['all_pass_rate'], reverse=True)
    return out


def operator_effectiveness(user_id: int, lookback_alphas: int = 200,
                            top_n: int = 8) -> list[dict[str, Any]]:
    """최근 lookback_alphas 개 알파를 outermost_operator 별로 집계.

    Python 측 그룹화 (alpha_ast.outermost_operator lazy import).
    min count>=2 필터, all_pass_rate 내림차순 → avg_pass_count 내림차순 정렬, top_n 반환.

    반환: [{'operator':str, 'count':int, 'all_pass_rate':float, 'avg_pass_count':float}]
    """
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT code, pass_count, fail_count, error_count '
            'FROM alphas WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (user_id, int(lookback_alphas)),
        ).fetchall()
    if not rows:
        return []

    from . import alpha_ast as _alpha_ast

    groups: dict[str, list[dict]] = {}
    for r in rows:
        op = _alpha_ast.outermost_operator(r['code'] or '')
        if op is None:
            continue
        groups.setdefault(op, []).append({
            'pass_count': int(r['pass_count'] or 0),
            'fail_count': int(r['fail_count'] or 0),
            'error_count': int(r['error_count'] or 0),
        })

    out = []
    for op, items in groups.items():
        n = len(items)
        if n < 2:
            continue
        n_ap = sum(
            1 for x in items
            if x['fail_count'] == 0 and x['error_count'] == 0 and x['pass_count'] >= 7
        )
        avg_pc = sum(x['pass_count'] for x in items) / n
        out.append({
            'operator': op,
            'count': n,
            'all_pass_rate': n_ap / n,
            'avg_pass_count': avg_pc,
        })

    # Sort: all_pass_rate DESC, then avg_pass_count DESC
    out.sort(key=lambda d: (d['all_pass_rate'], d['avg_pass_count']), reverse=True)
    return out[:top_n]


def round_reward_trend(user_id: int, window: int = 10) -> float:
    """최근 window 라운드의 round-level 평균 pass_count 를 시계열로 OLS degree-1 slope 산출.

    각 라운드의 '품질 proxy' = 해당 라운드 알파들의 평균 pass_count (cached 포함).
    slope > 0 → 개선 중, slope < 0 → 악화, 0.0 → <2 라운드 또는 flat.
    numpy 없이 직접 OLS 구현.
    """
    init()
    with _DB_LOCK, _connect() as conn:
        # 최근 window 라운드 번호 목록 (DESC)
        rn_rows = conn.execute(
            'SELECT DISTINCT round_num FROM alphas WHERE user_id=? '
            'ORDER BY round_num DESC LIMIT ?',
            (user_id, int(window)),
        ).fetchall()
    if len(rn_rows) < 2:
        return 0.0

    # 오래된 것부터 정렬
    round_nums = sorted(int(r['round_num']) for r in rn_rows)

    # 각 라운드의 평균 pass_count 계산
    with _DB_LOCK, _connect() as conn:
        placeholders = ','.join('?' * len(round_nums))
        avg_rows = conn.execute(
            f'SELECT round_num, AVG(pass_count) AS avg_pc FROM alphas '
            f'WHERE user_id=? AND round_num IN ({placeholders}) '
            f'GROUP BY round_num ORDER BY round_num ASC',
            (user_id, *round_nums),
        ).fetchall()
    if len(avg_rows) < 2:
        return 0.0

    # OLS: slope = (n*Σxy - Σx·Σy) / (n*Σx² - (Σx)²)
    # x = sequential index (0, 1, 2, ...), y = avg_pc
    xs = list(range(len(avg_rows)))
    ys = [float(r['avg_pc'] or 0) for r in avg_rows]
    n = len(xs)
    sum_x  = sum(xs)
    sum_y  = sum(ys)
    sum_xy = sum(xs[i] * ys[i] for i in range(n))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def directive_stats(user_id: int, window_edges: int = 800
                    ) -> dict[tuple[str, str], dict[str, Any]]:
    """정향변이 학습 관측 집계 — (부모 fail category, 적용 directive) → 성공 통계.

    부모→자식 귀속 엣지(alphas.parent_alpha_id + alphas.directive) 중 최근
    window_edges 개를 mutation_learn.outcome_observations 로 채점해 합산한다.
    반환: {(category, directive): {'n': int, 'wins': int, 'win_rate': float}}.
    엣지가 없으면 빈 dict — choose_directive 가 사전확률(규칙)로 폴백한다.
    """
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT c.directive AS directive, c.pass_count AS c_pass, '
            '  c.fail_items AS c_fail, c.error_text AS c_err, c.metrics AS c_met, '
            '  p.pass_count AS p_pass, p.fail_items AS p_fail, p.metrics AS p_met '
            'FROM alphas c JOIN alphas p ON p.id = c.parent_alpha_id '
            'WHERE c.user_id=? AND c.directive IS NOT NULL '
            "AND TRIM(c.directive) <> '' "
            'ORDER BY c.id DESC LIMIT ?',
            (user_id, int(window_edges)),
        ).fetchall()

    from . import mutation_learn as _ml

    def _j(s):
        try:
            v = json.loads(s or '{}')
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        try:
            p_fail = json.loads(r['p_fail'] or '[]')
            c_fail = json.loads(r['c_fail'] or '[]')
        except Exception:
            continue
        # metrics 를 반드시 함께 넘긴다 — outcome_observations 의 '표적 지표가 나아졌나'
        # 판정(부분 전진)이 이것 없이는 동작하지 않고, 그러면 학습이 전 축 0승으로 죽는다.
        obs = _ml.outcome_observations(
            {'fail_items': p_fail, 'pass_count': r['p_pass'],
             'metrics': _j(r['p_met'])},
            {'fail_items': c_fail, 'pass_count': r['c_pass'],
             'directive': r['directive'], 'error_text': r['c_err'],
             'metrics': _j(r['c_met'])})
        for cat, d, win in obs:
            st = out.setdefault((cat, d), {'n': 0, 'wins': 0})
            st['n'] += 1
            st['wins'] += 1 if win else 0
    for st in out.values():
        st['win_rate'] = (st['wins'] / st['n']) if st['n'] else 0.0
    return out


# ─────────────────────────────────────────────────────────────────────────────
# v7: 백엔드 능력 (역할 account_type 과 분리된 '측정된 사실')
# ─────────────────────────────────────────────────────────────────────────────

def get_backend(user_id: int) -> str:
    """'api' | '' (미탐침). 역할이 아니라 **측정된 전송 능력**이다.
    (구 'browser' 값은 Playwright 제거로 폐기 — 남은 행은 워커가 재탐침해 'api' 로 치유한다.)"""
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT backend FROM users WHERE id=?', (user_id,)).fetchone()
    return str((row['backend'] if row else '') or '')


@_with_conn
def set_backend(conn, user_id: int, backend: str) -> None:
    if backend not in ('api', ''):     # 'browser' 는 Playwright 제거로 폐기(2026-07-13)
        raise ValueError(f'invalid backend: {backend!r}')
    conn.execute('UPDATE users SET backend=? WHERE id=?', (backend, user_id))


def recent_fail_counts(user_id: int, limit: int = 400) -> dict[str, int]:
    """최근 `limit` 알파의 FAIL 체크 이름별 건수. 자율 이데이션이 '병목' 을 말할 근거."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT fail_items FROM alphas WHERE user_id=? '
            "AND TRIM(error_text)='' ORDER BY id DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        try:
            items = json.loads(r['fail_items'] or '[]')
        except Exception:
            continue
        for it in items:
            name = (it.get('name') if isinstance(it, dict) else str(it)) or ''
            name = str(name).strip()
            if name:
                out[name] = out.get(name, 0) + 1
    return out


def recent_family_counts(user_id: int, limit: int = 400) -> dict[str, int]:
    """최근 `limit` 알파가 쓴 유전체 family 별 건수. 탐색 공백(안 써본 패밀리) 근거."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT genome FROM alphas WHERE user_id=? AND genome IS NOT NULL '
            'ORDER BY id DESC LIMIT ?',
            (user_id, int(limit)),
        ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        try:
            g = json.loads(r['genome'] or '{}')
        except Exception:
            continue
        fam = str((g or {}).get('family') or '').strip()
        if fam:
            out[fam] = out.get(fam, 0) + 1
    return out


# ─────────────────────────────────────────────────────────────────────────────
# v7: 리서치 런 / 가설 / 전략스펙
# ─────────────────────────────────────────────────────────────────────────────

RESEARCH_STATUSES = ('pending', 'gathering', 'ideating', 'concretizing', 'ready', 'error')


def create_research_run(user_id: int, query: str) -> int:
    init()
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            'INSERT INTO research_runs (user_id, query, status, created_at, updated_at) '
            "VALUES (?,?,'pending',?,?)",
            (user_id, str(query or '').strip(), now, now),
        )
        return int(cur.lastrowid)


def update_research_run(run_id: int, *, status: str | None = None,
                        evidence: str | None = None, sources: list | None = None,
                        error: str | None = None) -> None:
    init()
    sets, vals = [], []
    if status is not None:
        if status not in RESEARCH_STATUSES:
            raise ValueError(f'invalid research status: {status!r}')
        sets.append('status=?'); vals.append(status)
    if evidence is not None:
        sets.append('evidence=?'); vals.append(str(evidence))
    if sources is not None:
        sets.append('sources=?'); vals.append(json.dumps(list(sources), ensure_ascii=False))
    if error is not None:
        sets.append('error=?'); vals.append(str(error)[:600])
    if not sets:
        return
    sets.append('updated_at=?'); vals.append(time.time())
    vals.append(run_id)
    with _DB_LOCK, _connect() as conn:
        conn.execute(f'UPDATE research_runs SET {", ".join(sets)} WHERE id=?', vals)


def _research_view(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d['sources'] = json.loads(d.get('sources') or '[]')
    except Exception:
        d['sources'] = []
    return d


def get_research_run(run_id: int) -> dict[str, Any] | None:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute('SELECT * FROM research_runs WHERE id=?', (run_id,)).fetchone()
    return _research_view(row) if row else None


def latest_research_run(user_id: int) -> dict[str, Any] | None:
    init()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            'SELECT * FROM research_runs WHERE user_id=? ORDER BY id DESC LIMIT 1',
            (user_id,)).fetchone()
    return _research_view(row) if row else None


def insert_hypothesis(run_id: int, user_id: int, h: dict[str, Any]) -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            'INSERT INTO hypotheses (run_id, user_id, title, rationale, citations, '
            'family_hint, created_at) VALUES (?,?,?,?,?,?,?)',
            (run_id, user_id, str(h.get('title') or '')[:200],
             str(h.get('rationale') or '')[:2000],
             json.dumps(list(h.get('citations') or []), ensure_ascii=False),
             str(h.get('family_hint') or '')[:40], time.time()),
        )
        return int(cur.lastrowid)


def list_hypotheses(run_id: int) -> list[dict[str, Any]]:
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM hypotheses WHERE run_id=? ORDER BY id ASC', (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['citations'] = json.loads(d.get('citations') or '[]')
        except Exception:
            d['citations'] = []
        out.append(d)
    return out


def insert_spec(hypothesis_id: int, user_id: int, *, genome: dict, code: str,
                settings: dict | None = None, delay=None, why: str = '') -> int:
    init()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            'INSERT INTO strategy_specs (hypothesis_id, user_id, genome, code, settings, '
            "delay, status, why, created_at) VALUES (?,?,?,?,?,?,'pending',?,?)",
            (hypothesis_id, user_id,
             json.dumps(dict(genome), ensure_ascii=False), str(code),
             json.dumps(dict(settings or {}), ensure_ascii=False),
             int(delay) if str(delay).lstrip('-').isdigit() else None,
             str(why or '')[:500], time.time()),
        )
        return int(cur.lastrowid)


def _spec_view(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k, empty in (('genome', '{}'), ('settings', '{}')):
        try:
            d[k] = json.loads(d.get(k) or empty)
        except Exception:
            d[k] = {}
    return d


def pending_specs(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    """GA 가 다음 라운드에 소비할 미시딩 전략스펙 (오래된 것부터)."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_specs WHERE user_id=? AND status='pending' "
            'ORDER BY id ASC LIMIT ?', (user_id, int(limit))).fetchall()
    return [_spec_view(r) for r in rows]


def mark_specs(spec_ids, status: str, *, seeded_round: int | None = None) -> None:
    ids = [int(i) for i in (spec_ids or [])]
    if not ids:
        return
    if status not in ('pending', 'seeded', 'exhausted', 'rejected'):
        raise ValueError(f'invalid spec status: {status!r}')
    init()
    ph = ','.join('?' * len(ids))
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            f'UPDATE strategy_specs SET status=?, seeded_round=? WHERE id IN ({ph})',
            (status, seeded_round, *ids))


@_with_conn
def attach_spec_alpha(conn, spec_id: int, alpha_id: int) -> None:
    conn.execute('UPDATE strategy_specs SET alpha_id=? WHERE id=?',
                 (int(alpha_id), int(spec_id)))


def spec_counts(user_id: int) -> dict[str, int]:
    """상태별 스펙 수 — 대시보드 진행률."""
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT status, COUNT(*) AS n FROM strategy_specs WHERE user_id=? '
            'GROUP BY status', (user_id,)).fetchall()
    return {str(r['status']): int(r['n']) for r in rows}


def list_specs_for_run(run_id: int) -> list[dict[str, Any]]:
    init()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT s.* FROM strategy_specs s JOIN hypotheses h ON h.id = s.hypothesis_id '
            'WHERE h.run_id=? ORDER BY s.id ASC', (run_id,)).fetchall()
    return [_spec_view(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# P4 meta-strategy helpers — read-only SELECTs, no schema change
# ─────────────────────────────────────────────────────────────────────────────

def recent_round_scores(user_id: int, n: int = 6) -> list[float]:
    """Last n 'done' rounds for user_id, oldest-first, as list[float].

    The per-round score is the round's BEST alpha quality = MAX(alphas.pass_count)
    over that round — NOT rounds.pass_count (which counts only PASS>=threshold and
    is 0 for near-miss focus sub-rounds, giving a useless flat trajectory). Using
    the per-round max captures near-miss progress (e.g. 6,5,6,4) so the meta-strategy
    can detect plateaus. Returns [] on any error / no done rounds. Oldest-first.
    """
    try:
        init()
        with _DB_LOCK, _connect() as conn:
            rows = conn.execute(
                'SELECT COALESCE(MAX(a.pass_count), 0) AS best '
                'FROM rounds r LEFT JOIN alphas a ON a.round_id = r.id '
                "WHERE r.user_id=? AND r.status='done' "
                'GROUP BY r.id ORDER BY r.id DESC LIMIT ?',
                (user_id, int(n)),
            ).fetchall()
        # rows are newest-first; reverse to oldest-first
        return [float(r['best']) for r in reversed(rows)]
    except Exception:
        return []


def survivor_alphas(user_id: int, n: int = 6, min_pass: int = 5) -> list[dict]:
    """Up to n recent distinct-code alphas with pass_count >= min_pass, newest first.

    Each dict: {'code': str, 'pass_count': int, 'operators': list[str]}.
    operators = sorted(alpha_ast.operators_used(code)).
    Dedup by code (keep newest). Returns [] on any error.
    """
    try:
        init()
        with _DB_LOCK, _connect() as conn:
            rows = conn.execute(
                'SELECT code, pass_count FROM alphas '
                'WHERE user_id=? AND pass_count>=? '
                'ORDER BY id DESC LIMIT ?',
                (user_id, int(min_pass), int(n) * 3),
            ).fetchall()
        try:
            from . import alpha_ast as _alpha_ast
            _ops_fn = _alpha_ast.operators_used
        except Exception:
            _ops_fn = None

        seen: set[str] = set()
        out: list[dict] = []
        for r in rows:
            code = r['code'] or ''
            if not code or code in seen:
                continue
            seen.add(code)
            if _ops_fn is not None:
                try:
                    ops: list[str] = sorted(_ops_fn(code))
                except Exception:
                    ops = []
            else:
                ops = []
            out.append({
                'code': code,
                'pass_count': int(r['pass_count']),
                'operators': ops,
            })
            if len(out) >= int(n):
                break
        return out
    except Exception:
        return []


# ── 위원회(committee) 근거 + 제출 재시도 헬퍼 (2026-07-23) ───────────────────

def pocket_stats(user_id: int, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    """최근 N일 (delay, universe, neutralization) 구역별 실측 요약 — 위원회 근거용.

    n 내림차순. avg_sharpe 는 소수 3자리 문자열이 아니라 float (LLM 프롬프트에 그대로
    들어가므로 round 처리), hi = |sharpe| >= 1.25 개수(D1 표준컷 1.58 의 예열 지표).
    """
    init()
    since = time.time() - days * 86400.0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            'SELECT delay, universe, neutralization, COUNT(*) n, '
            'ROUND(AVG(sharpe), 3) avg_sharpe, '
            'SUM(CASE WHEN ABS(sharpe) >= 1.25 THEN 1 ELSE 0 END) hi '
            'FROM alphas WHERE user_id=? AND ts>? AND sharpe IS NOT NULL '
            'GROUP BY delay, universe, neutralization '
            'ORDER BY n DESC LIMIT ?',
            (user_id, since, int(limit)),
        ).fetchall()
    return [{'delay': r['delay'], 'universe': r['universe'] or '?',
             'neutralization': r['neutralization'] or '?', 'n': int(r['n']),
             'avg_sharpe': r['avg_sharpe'], 'hi': int(r['hi'] or 0)} for r in rows]


def rejection_stats(user_id: int, days: int = 7, limit: int = 12) -> dict[str, int]:
    """최근 N일 제출 거절/보류 사유 상위 — 위원회의 탈상관 심사역이 주로 읽는다."""
    init()
    since = time.time() - days * 86400.0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT submit_status s, COUNT(*) n FROM submit_attempts "
            "WHERE user_id=? AND ts>? AND TRIM(submit_status) <> '' "
            "AND submit_status NOT LIKE 'submit_skipped:paused%' "
            'GROUP BY submit_status ORDER BY n DESC LIMIT ?',
            (user_id, since, int(limit)),
        ).fetchall()
    return {str(r['s'])[:120]: int(r['n']) for r in rows}


# 제출 재시도 대상 상태 — **일시 장애**만. rejected: 는 WQB 의 확정 판정이라 제외.
_STUCK_SUBMIT_PREFIXES = ('submit_pending_timeout', 'submit_http_502',
                          'submit_http_504', 'submit_http_429', 'submit_http_500')


def stuck_submits(user_id: int, *, since_s: float = 172800.0,
                  limit: int = 5) -> list[dict[str, Any]]:
    """일시 장애로 제출이 끊겼지만 **차단 FAIL 0** 이었던 알파 — 재시도 후보.

    같은 code_hash 로 이미 제출 성공(submitted=1)했거나 확정 거절(rejected:)된 행이
    있으면 제외한다 — 같은 식을 다시 내면 어차피 같은 판정이다.
    최신 우선. metrics 는 JSON 파싱해 dict 로 돌려준다 (wqb_alpha_id 가 열쇠).
    """
    init()
    since = time.time() - float(since_s)
    conds = ' OR '.join(f"submit_status LIKE '{p}%'" for p in _STUCK_SUBMIT_PREFIXES)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f'SELECT id, code, code_hash, metrics, genome, submit_status, ts, fail_count, '
            f'error_count FROM alphas a WHERE user_id=? AND ts>? AND submitted=0 '
            f'AND ({conds}) AND fail_count=0 AND error_count=0 '
            "AND NOT EXISTS (SELECT 1 FROM alphas b WHERE b.user_id=a.user_id "
            'AND b.code_hash=a.code_hash AND (b.submitted=1 OR '
            "b.submit_status LIKE 'rejected:%')) "
            'ORDER BY id DESC LIMIT ?',
            (user_id, since, int(limit)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for r in rows:
        ch = str(r['code_hash'] or '')
        if ch in seen_codes:
            continue
        seen_codes.add(ch)
        try:
            metrics = json.loads(r['metrics']) if r['metrics'] else {}
        except (TypeError, ValueError):
            metrics = {}
        try:
            genome = json.loads(r['genome']) if r['genome'] else None
        except (TypeError, ValueError):
            genome = None
        out.append({'id': int(r['id']), 'code': r['code'], 'code_hash': ch,
                    'metrics': metrics if isinstance(metrics, dict) else {},
                    'genome': genome if isinstance(genome, dict) else None,
                    'submit_status': r['submit_status'], 'ts': r['ts']})
    return out


def set_alpha_submit_result(alpha_pk: int, submitted: bool,
                            submit_status: str) -> None:
    """재시도한 제출의 최종 상태를 해당 알파 행에 기록한다."""
    init()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            'UPDATE alphas SET submitted=?, submit_status=? WHERE id=?',
            (1 if submitted else 0, str(submit_status or '')[:300], int(alpha_pk)))


def rejected_fieldsets(user_id: int, *, since_s: float = 86400.0,
                       min_count: int = 3) -> list[frozenset]:
    """최근 N시간 동안 제출이 `rejected:` 로 min_count 회 이상 끝난 **필드 조합**들.

    2026-07-24 실측: 같은 mdl177 3종 필드셋의 변형 19개가 3시간 동안 전부
    PROD_CORRELATION/LOW_2Y 로 거절됐다 — 상관은 필드(아이디어) 수준 속성이라
    중립화·감쇠만 바꾼 형제는 같은 벽에 부딪힌다. 이 목록이 제출 게이트의
    쿨다운 근거가 된다 (시뮬은 계속 하되 **제출만** 보류 — 학습 데이터는 쌓인다).
    """
    init()
    since = time.time() - float(since_s)
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT genome FROM alphas WHERE user_id=? AND ts>? "
            "AND submit_status LIKE 'rejected:%' AND genome IS NOT NULL",
            (user_id, since),
        ).fetchall()
    cnt: dict[frozenset, int] = {}
    for r in rows:
        try:
            g = json.loads(r['genome'])
            fs = frozenset(str(f) for f in (g.get('fields') or []) if f)
        except (TypeError, ValueError):
            continue
        if fs:
            cnt[fs] = cnt.get(fs, 0) + 1
    return [fs for fs, n in cnt.items() if n >= int(min_count)]
