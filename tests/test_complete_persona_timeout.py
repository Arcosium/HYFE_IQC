# tests/test_complete_persona_timeout.py
# finalize POST 가 네트워크에서 죽어도(타임아웃/504 후 재시도 등) 서버측에 이미
# 착지했을 수 있다 — 2026-07-31 실측: 30초 read timeout 뒤 세션은 인증돼 있었고
# UI 는 "인증이 완료되지 않았습니다" 를 거짓으로 띄웠다.
import requests

from server import wqb_api


def _client(tmp_path):
    c = wqb_api.WqbApiClient('u@x.com', 'pw', session_file=str(tmp_path / 's.pkl'))
    (tmp_path / 's.pkl.pending').write_text(
        '{"cookies": {}, "persona_url": '
        '"https://api.worldquantbrain.com/authentication/persona?inquiry=inq_TEST"}')
    return c


def test_timeout_with_valid_session_is_success(tmp_path, monkeypatch):
    c = _client(tmp_path)
    monkeypatch.setattr(c.session, 'post',
                        lambda *a, **k: (_ for _ in ()).throw(requests.Timeout('read timed out')))
    monkeypatch.setattr(c, '_session_valid', lambda: True)
    monkeypatch.setattr(c, '_ensure_expiry', lambda force=False: None)
    assert c.complete_persona() is True
    assert c.persona_required is False and c._authed is True
    assert not (tmp_path / 's.pkl.pending').exists()   # pending 정리됨


def test_timeout_with_dead_session_is_failure(tmp_path, monkeypatch):
    c = _client(tmp_path)
    monkeypatch.setattr(c.session, 'post',
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError('boom')))
    monkeypatch.setattr(c, '_session_valid', lambda: False)
    assert c.complete_persona() is False
    assert (tmp_path / 's.pkl.pending').exists()       # challenge 는 보존 — 재시도 가능
