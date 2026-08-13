# tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def _no_live_gate_profile(monkeypatch):
    """게이트 실측 프로파일(run_config)을 테스트에서 격리한다.

    `criteria.is_blocking` 은 gate_watch 의 **실측**을 하드코딩 규칙보다 우선한다.
    그래서 라이브가 새 증거를 학습하면 코드를 한 줄도 안 고쳤는데 테스트가 깨진다 —
    2026-08-13 13:21 에 실측이 LOW_SHARPE·LOW_FITNESS·LOW_2Y_SHARPE 를 soft 로
    갱신하자(거절 177 · 제출 28 표본) 관련 없는 테스트 4개가 그 자리에서 빨개졌다.

    기본값은 '실측 없음' — 테스트는 하드코딩 규칙을 검증한다. 실측 동작을 보려는
    테스트는 이 fixture 를 덮어써서 원하는 프로파일을 직접 넣는다.
    """
    from server import run_config
    monkeypatch.setattr(run_config, 'get_gate_profile', lambda: {}, raising=False)
