import pytest

from server import local_llm


def test_requires_base_url(monkeypatch):
    monkeypatch.delenv('IQC_LOCAL_LLM_BASE_URL', raising=False)
    with pytest.raises(local_llm.LocalLLMError, match='IQC_LOCAL_LLM_BASE_URL'):
        local_llm.generate_json(system_instruction='system', user_prompt='prompt', temperature=0.1)


def test_uses_openai_compatible_endpoint_without_auth(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': '[{"code":"rank(close)"}]'}}]}

    def fake_post(url, **kwargs):
        captured['url'] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv('IQC_LOCAL_LLM_BASE_URL', 'http://127.0.0.1:8080/v1')
    monkeypatch.setattr(local_llm.requests, 'post', fake_post)
    assert local_llm.generate_json(system_instruction='system', user_prompt='prompt', temperature=0.4) == '[{"code":"rank(close)"}]'
    assert captured['url'] == 'http://127.0.0.1:8080/v1/chat/completions'
    assert 'headers' not in captured
    assert captured['json']['model'] == local_llm.DEFAULT_MODEL
