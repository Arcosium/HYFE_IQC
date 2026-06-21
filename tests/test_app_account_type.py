# tests/test_app_account_type.py
"""Task 9: account_type signup + RC upgrade endpoint tests.

Run: python3.11 -m pytest tests/test_app_account_type.py -v
"""
import json
import pytest

import server.app as app_mod
from flask import jsonify


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


# ── Test 1: Signup passes account_type to validate_login and upsert_user ────

def test_signup_passes_account_type(client, monkeypatch):
    """신규 가입 시 account_type='research_consultant' 가 validate_login 과
    upsert_user 에 정확히 전달되어야 한다."""
    captured = {}

    # 신규 사용자 — 찾을 수 없음
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: None)

    def fake_validate_login(u, p, g, account_type='standard'):
        captured['validate_account_type'] = account_type
        return {'ok': True, 'reason': 'ok'}

    def fake_upsert_user(u, p, g, account_type='standard'):
        captured['upsert_account_type'] = account_type
        return 42  # fake uid

    def fake_issue_session(uid, wqb_username, remember):
        with app_mod.app.app_context():
            return jsonify({'ok': True, 'user_id': uid, 'wqb_username': wqb_username})

    monkeypatch.setattr(app_mod._auth, 'validate_login', fake_validate_login)
    monkeypatch.setattr(app_mod._db, 'upsert_user', fake_upsert_user)
    monkeypatch.setattr(app_mod, '_issue_session', fake_issue_session)

    r = client.post('/api/login', data=json.dumps({
        'wqb_username': 'user@example.com',
        'wqb_password': 'pass123',
        'gemini_api_key': 'AIzaSytest',
        'account_type': 'research_consultant',
    }), content_type='application/json')

    assert r.status_code == 200
    assert captured.get('validate_account_type') == 'research_consultant', \
        f"validate_login 에 account_type 미전달: {captured}"
    assert captured.get('upsert_account_type') == 'research_consultant', \
        f"upsert_user 에 account_type 미전달: {captured}"


# ── Test 2: Upgrade success ──────────────────────────────────────────────────

def test_upgrade_to_rc_success(client, monkeypatch):
    """로그인된 사용자가 WQB API 검증 통과 시 RC로 전환되어야 한다."""
    captured = {}

    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 7)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('user@ex.com', 'pw', 'gemkey'))
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api', lambda u, p: {'ok': True, 'reason': 'ok'})

    def fake_set_account_type(uid, account_type):
        captured['uid'] = uid
        captured['account_type'] = account_type

    monkeypatch.setattr(app_mod._db, 'set_account_type', fake_set_account_type)

    r = client.post('/api/account/upgrade-to-rc', content_type='application/json')
    data = r.get_json()

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {data}"
    assert data.get('ok') is True, f"Response not ok: {data}"
    assert captured.get('uid') == 7, f"set_account_type called with wrong uid: {captured}"
    assert captured.get('account_type') == 'research_consultant', \
        f"set_account_type called with wrong type: {captured}"


# ── Test 3: Upgrade rejected when WQB API says not consultant ───────────────

def test_upgrade_to_rc_rejected(client, monkeypatch):
    """WQB API 가 403 (not_consultant) 반환 시 400 응답, set_account_type 미호출."""
    set_called = []

    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 5)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('u@ex.com', 'pw', 'k'))
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api',
                        lambda u, p: {'ok': False, 'reason': 'wqb_not_consultant', 'detail': 'Not a consultant'})
    monkeypatch.setattr(app_mod._db, 'set_account_type', lambda uid, t: set_called.append((uid, t)))

    r = client.post('/api/account/upgrade-to-rc', content_type='application/json')
    data = r.get_json()

    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {data}"
    assert data.get('ok') is False or data.get('reason') == 'wqb_not_consultant', \
        f"Unexpected response: {data}"
    assert len(set_called) == 0, f"set_account_type should NOT have been called, but was: {set_called}"
