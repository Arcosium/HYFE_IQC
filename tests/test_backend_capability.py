"""백엔드 능력 탐지 — '403 = RC 전용' 가정을 측정으로 대체.

시뮬 백엔드는 WQB REST API 단일이다 (2026-07-13 Playwright/브라우저 경로 제거).
검증: probe 가 응답 코드를 backend 로 올바로 매핑하는가, validate_login 이 API 결과를
그대로 전하는가, simulate_batch 가 ApiBackend 로 붙는가.

Run: python3 -m pytest tests/test_backend_capability.py -v
"""
import pytest

from server import auth, db, wqb_backend


# ── probe_wqb_backend — 응답 → 능력 매핑 ─────────────────────────────────────

def _probe_with(monkeypatch, api_result):
    monkeypatch.setattr(auth, 'validate_wqb_api', lambda u, p: api_result)
    return auth.probe_wqb_backend('u', 'p')


def test_probe_success_is_api(monkeypatch):
    r = _probe_with(monkeypatch, {'ok': True, 'reason': 'ok'})
    assert r['backend'] == 'api'


def test_probe_persona_is_api(monkeypatch):
    """persona 는 계정 무관 — 일반 계정도 완료하면 API 를 쓴다. 능력은 'api'."""
    r = _probe_with(monkeypatch, {'ok': False, 'reason': 'wqb_persona_required',
                                  'persona_url': 'https://withpersona.com/x'})
    assert r['backend'] == 'api'
    assert r['persona_url'] == 'https://withpersona.com/x'


def test_probe_403_is_api_forbidden(monkeypatch):
    """403 → 'api_forbidden' (브라우저 폴백 제거됨). 등록은 거절된다."""
    r = _probe_with(monkeypatch, {'ok': False, 'reason': 'wqb_not_consultant'})
    assert r['backend'] == 'api_forbidden'


def test_probe_bad_credentials_is_undetermined(monkeypatch):
    r = _probe_with(monkeypatch, {'ok': False, 'reason': 'wqb_credentials'})
    assert r['backend'] == ''


def test_probe_rate_limited_is_undetermined(monkeypatch):
    """429 는 '판정 보류' — persist 하면 안 된다(다음에 재탐침)."""
    r = _probe_with(monkeypatch, {'ok': False, 'reason': 'wqb_rate_limited'})
    assert r['backend'] == ''


# ── validate_login — API 결과를 그대로 전한다 (브라우저 폴백 없음) ─────────────

def test_validate_login_api_success(monkeypatch):
    monkeypatch.setattr(auth, 'validate_wqb_api', lambda u, p: {'ok': True, 'reason': 'ok'})
    r = auth.validate_login('u', 'p')
    assert r['ok'] and r['backend'] == 'api'


def test_validate_login_persona_passes_through(monkeypatch):
    monkeypatch.setattr(auth, 'validate_wqb_api',
                        lambda u, p: {'ok': False, 'reason': 'wqb_persona_required',
                                      'persona_url': 'https://withpersona.com/x'})
    r = auth.validate_login('u', 'p')
    assert r['backend'] == 'api'
    assert r['reason'] == 'wqb_persona_required'


def test_validate_login_403_is_rejected(monkeypatch):
    """403 → 브라우저 폴백 없이 거절(ok=False, api_forbidden)."""
    monkeypatch.setattr(auth, 'validate_wqb_api',
                        lambda u, p: {'ok': False, 'reason': 'wqb_not_consultant'})
    r = auth.validate_login('u', 'p')
    assert not r['ok']
    assert r['backend'] == 'api_forbidden'
    assert r['reason'] == 'wqb_not_consultant'


def test_validate_login_bad_creds_passes_through(monkeypatch):
    monkeypatch.setattr(auth, 'validate_wqb_api',
                        lambda u, p: {'ok': False, 'reason': 'wqb_credentials'})
    r = auth.validate_login('u', 'p')
    assert not r['ok'] and r['reason'] == 'wqb_credentials'


# ── simulate_batch — 항상 ApiBackend 로 붙는다 ───────────────────────────────

def test_simulate_batch_uses_api_backend(monkeypatch):
    calls = {}

    class _FakeApi:
        def __init__(self, *a, **kw):
            pass

        def simulate_batch(self, strategies, **kw):
            calls['path'] = 'api'
            return [{'idx': 1}]

    monkeypatch.setattr(wqb_backend, 'ApiBackend', _FakeApi)
    wqb_backend.simulate_batch([{'idx': 1}], wqb_username='u', wqb_password='p',
                               account_type='standard')
    assert calls['path'] == 'api'


def test_simulate_batch_ignores_legacy_backend_kwarg(monkeypatch):
    """구 호출부가 account_type/backend 를 넘겨도 무시하고 항상 API 로 붙는다."""
    calls = {}

    class _FakeApi:
        def __init__(self, *a, **kw):
            pass

        def simulate_batch(self, strategies, **kw):
            calls['path'] = 'api'
            return []

    monkeypatch.setattr(wqb_backend, 'ApiBackend', _FakeApi)
    wqb_backend.simulate_batch([{'idx': 1}], wqb_username='u', wqb_password='p',
                               account_type='research_consultant', backend='browser')
    assert calls['path'] == 'api'


# ── DB backend 컬럼 ──────────────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'be.db'))
    db._INITIALIZED = False
    db.init()
    yield
    db._INITIALIZED = False


def test_backend_defaults_empty_and_roundtrips(isolated_db):
    uid = db.upsert_user('be@t.com', 'p', '', account_type='standard')
    assert db.get_backend(uid) == ''      # 미탐침
    db.set_backend(uid, 'api')
    assert db.get_backend(uid) == 'api'


def test_rc_backfilled_to_api(isolated_db):
    """RC 는 오늘 이미 API 로 도는 게 증명돼 있으므로 마이그레이션이 백필한다."""
    uid = db.upsert_user('rc@t.com', 'p', '', account_type='research_consultant')
    assert db.get_backend(uid) == 'api'


def test_set_backend_rejects_garbage(isolated_db):
    uid = db.upsert_user('x@t.com', 'p', '')
    with pytest.raises(ValueError):
        db.set_backend(uid, 'quantum')


def test_set_backend_rejects_browser(isolated_db):
    """'browser' 는 Playwright 제거로 폐기된 값 — 이제 거절된다."""
    uid = db.upsert_user('b@t.com', 'p', '')
    with pytest.raises(ValueError):
        db.set_backend(uid, 'browser')
