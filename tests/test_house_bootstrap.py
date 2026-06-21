# tests/test_house_bootstrap.py
"""_ensure_house_rc_account() 단위 테스트 (4케이스).

app 모듈 import는 side-effect-free이므로 안전하게 import 후 monkeypatch 사용.
"""
import server.app as app_mod


def test_promotes_standard_user(monkeypatch):
    """DB에 하우스 유저가 있고 account_type이 'standard'이면 'research_consultant'로 보정."""
    calls = []
    monkeypatch.setattr(app_mod._db, 'get_user_id_by_username', lambda _: 42)
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda _: 'standard')
    monkeypatch.setattr(app_mod._db, 'set_account_type', lambda uid, t: calls.append((uid, t)))

    app_mod._ensure_house_rc_account()

    assert calls == [(42, 'research_consultant')]


def test_no_house_user_noop(monkeypatch):
    """DB에 하우스 유저가 없으면 set_account_type 호출 없음."""
    calls = []
    monkeypatch.setattr(app_mod._db, 'get_user_id_by_username', lambda _: None)
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda _: 'standard')
    monkeypatch.setattr(app_mod._db, 'set_account_type', lambda uid, t: calls.append((uid, t)))

    app_mod._ensure_house_rc_account()

    assert calls == []


def test_already_rc_idempotent(monkeypatch):
    """이미 'research_consultant'이면 set_account_type 호출 없음."""
    calls = []
    monkeypatch.setattr(app_mod._db, 'get_user_id_by_username', lambda _: 99)
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda _: 'research_consultant')
    monkeypatch.setattr(app_mod._db, 'set_account_type', lambda uid, t: calls.append((uid, t)))

    app_mod._ensure_house_rc_account()

    assert calls == []


def test_never_raises_on_db_exception(monkeypatch):
    """DB 조회가 예외를 던져도 부팅을 막으면 안 됨 — 절대 raise 하지 않아야 한다."""
    def boom(_):
        raise RuntimeError('DB locked')

    monkeypatch.setattr(app_mod._db, 'get_user_id_by_username', boom)

    # 예외가 전파되지 않아야 한다
    app_mod._ensure_house_rc_account()
