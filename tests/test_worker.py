# tests/test_worker.py
import server.worker as worker


def test_round_label_base_round():
    """탐색(base) 라운드 = 정수 라벨 그대로 (phase 0)."""
    assert worker._round_label(3, 0, 0) == '3'
    assert worker._round_label(701, 0, 0) == '701'


def test_round_label_focus_includes_parent_alpha_and_depth():
    """focus 라운드 = {base}-{부모알파}-{개선깊이} (예: 2-2-3)."""
    assert worker._round_label(2, 2, 3) == '2-2-3'
    assert worker._round_label(701, 5, 1) == '701-5-1'
    # phase(깊이) 가 0 이면 base 취급 — 부모 정보 없음.
    assert worker._round_label(4, 7, 0) == '4'


# ── auth-fail 감지 (세션 만료 시 워커 정지 → /authentication 폭주 방지) ──

def test_is_auth_required_rc_persona():
    assert worker._is_auth_required('WQB biometric(Persona) 인증 필요 — 대시보드에서 완료') is True


def test_is_auth_required_rc_creds_or_429_fail():
    # RC 세션 만료/429 시 ApiBackend 가 내는 메시지 — 워커가 멈춰야 한다.
    assert worker._is_auth_required('WQB API 인증 실패 (RC 자격증명/권한 확인)') is True


def test_is_auth_required_new_device_still_true():
    assert worker._is_auth_required('WQB 가 new device 인증을 요구') is True


def test_is_auth_required_concurrent_sim_limit_is_false():
    # 슬롯 429(CONCURRENT_SIMULATION_LIMIT)=인증문제 아님. patient-retry 담당, 워커 멈추면 안 됨.
    assert worker._is_auth_required('CONCURRENT_SIMULATION_LIMIT_EXCEEDED (429)') is False


def test_is_auth_required_normal_sim_error_false():
    assert worker._is_auth_required('sim ERROR: bad expression at index 1') is False
