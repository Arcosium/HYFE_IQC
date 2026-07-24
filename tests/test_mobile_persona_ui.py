"""모바일 페이지(static/mobile.html)의 WQB biometric(Persona) 인증 UI 계약.

2026-07-10 사장 보고: "앱/모바일에서는 '여기서 인증 완료하기'·'인증 완료했습니다 — 세션 저장'
박스가 안 보여서 인증을 끝내도 세션을 저장할 수 없다." 실제로 mobile.html 에는 persona 관련
마크업이 아예 없었다(데스크톱 index.html + app.js 에만 있었다).

⚠ 불변식: 모바일 페이지는 `/api/account/wqb-persona-status` 를 **폴링하면 안 된다**.
세션·pending 이 둘 다 없으면 그 엔드포인트가 WQB 로 POST /authentication 을 날려
BIOMETRICS_THROTTLED(429) 가 영구 재무장된다. 진입 시 1회만 호출한다.
(주기 확인이 필요하면 passive 한 `/api/account/wqb-persona-watch` 를 쓴다.)
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile.html").read_text(encoding="utf-8")


def test_mobile_has_the_persona_box():
    for needle in ('id="persona-banner"', 'id="persona-link"',
                   'id="btn-persona-complete"', 'id="persona-status"'):
        assert needle in MOBILE, f"모바일에 {needle} 가 없다"
    assert "여기서 인증 완료하기" in MOBILE
    assert "인증 완료했습니다 — 세션 저장" in MOBILE


def test_mobile_calls_the_persona_endpoints():
    assert "/api/account/wqb-persona-status" in MOBILE      # 진입 시 1회 — challenge 유무 확인
    assert "/api/account/wqb-persona-link" in MOBILE        # 링크 누를 때 1회 — 그 시점에 발급
    assert "/api/account/wqb-persona-complete" in MOBILE    # 완료 → 세션 저장


def test_mobile_never_prefetches_the_persona_link():
    """링크를 미리 받아 href 에 꽂아두면 안 된다.

    링크 발급은 inquiry 를 재개시켜 직전 Persona 세션을 무효화한다. 미리 받아두면 그 사이
    다른 조회가 링크를 갈아치워 사용자가 연 인증 페이지가 무한 새로고침된다
    (사장 보고 2026-07-10). 반드시 클릭 핸들러에서 /wqb-persona-link 로 받아 바로 연다.
    """
    assert "personaLink.href =" not in MOBILE, "링크를 미리 href 에 꽂고 있다"
    # persona-link 호출은 클릭 핸들러 안에서만 일어난다.
    for m in re.finditer(r"set(?:Interval|Timeout)\s*\(", MOBILE):
        window = MOBILE[m.start():m.start() + 400]
        assert "wqb-persona-link" not in window, "persona-link 를 타이머로 호출하고 있다"


def test_mobile_never_polls_persona_status():
    """setInterval/setTimeout 로 persona-status 를 반복 호출하면 throttle 이 영구 재무장된다."""
    for m in re.finditer(r"set(?:Interval|Timeout)\s*\(", MOBILE):
        window = MOBILE[m.start():m.start() + 400]
        assert "wqb-persona-status" not in window, "persona-status 를 타이머로 호출하고 있다"
    # checkPersonaStatus 호출 지점은 로그인 성공 직후와 자동로그인 직후 두 곳뿐이다.
    assert MOBILE.count("checkPersonaStatus();") == 2


def test_both_pages_expose_an_explicit_relink_button():
    """인증 페이지가 'session expired' 로 죽었을 때 사용자가 스스로 새 링크를 받을 수
    있어야 한다(2026-07-22 사장 지시). 재발급은 자동이 될 수 없다 — 직전 링크를
    무효화하므로 사용자가 누른 순간에만 나가야 하고, 그래서 버튼이 필요하다."""
    desktop_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    desktop_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for name, blob in (("mobile.html", MOBILE), ("index.html", desktop_html)):
        assert 'id="btn-persona-renew"' in blob, f"{name} 에 재발급 버튼이 없다"
        assert 'id="btn-persona-open"' in blob, f"{name} 에 재인증 진입점이 없다"
    # 재발급은 force 플래그로만 서버에 전달된다(그냥 링크 요청은 살아있는 challenge 를 유지).
    assert "force: true" in MOBILE
    assert "force: true" in desktop_js


def test_relink_is_never_automatic():
    """force 재발급을 타이머/진입 시점에 부르면 열려 있는 인증 페이지가 죽는다."""
    desktop_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for blob in (MOBILE, desktop_js):
        for m in re.finditer(r"set(?:Interval|Timeout)\s*\(", blob):
            window = blob[m.start():m.start() + 400]
            assert "requestPersonaLink" not in window, "링크 발급을 타이머로 부르고 있다"
        assert "requestPersonaLink(true)" in blob
        # force 호출은 클릭 핸들러 안에서만 — 정확히 한 곳.
        assert blob.count("requestPersonaLink(true)") == 1


def test_mobile_only_opens_public_persona_urls():
    """서버가 걸러 주지만, 링크에 임의 URL 이 꽂히지 않도록 클라이언트도 확인한다."""
    assert "withpersona.com" in MOBILE
    assert 'aria-disabled' in MOBILE
