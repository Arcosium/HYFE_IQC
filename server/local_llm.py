"""Local OpenAI-compatible LLM client.

The model server is intentionally configured only through environment variables:
  IQC_LOCAL_LLM_BASE_URL  e.g. http://127.0.0.1:8080/v1
  IQC_LOCAL_LLM_MODEL     optional override for the loaded model name
No Authorization header or API key is used.
"""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_MODEL = 'Qwen3.6-35B-A3B-Uncensored-Claude-Genesis-Q8_0.gguf'


class LocalLLMError(RuntimeError):
    pass


def model_name() -> str:
    return os.environ.get('IQC_LOCAL_LLM_MODEL', '').strip() or DEFAULT_MODEL


def _endpoint() -> str:
    base_url = os.environ.get('IQC_LOCAL_LLM_BASE_URL', '').strip().rstrip('/')
    if not base_url:
        raise LocalLLMError(
            'IQC_LOCAL_LLM_BASE_URL is not set. Set it to the local model server, '
            'for example http://127.0.0.1:8080/v1.'
        )
    return base_url if base_url.endswith('/chat/completions') else f'{base_url}/chat/completions'


def generate_json(*, system_instruction: str, user_prompt: str,
                  temperature: float, max_tokens: int = 8192) -> str:
    """Call a local OpenAI-compatible chat-completions endpoint without credentials."""
    payload: dict[str, Any] = {
        'model': model_name(),
        'messages': [
            {'role': 'system', 'content': system_instruction},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
        'response_format': {'type': 'json_object'},
    }
    try:
        response = requests.post(_endpoint(), json=payload, timeout=(10, 300))
        response.raise_for_status()
        data = response.json()
        text = ((data.get('choices') or [{}])[0].get('message') or {}).get('content')
        if not isinstance(text, str) or not text.strip():
            raise LocalLLMError('local model returned an empty chat completion')
        return text.strip()
    except LocalLLMError:
        raise
    except Exception as exc:
        raise LocalLLMError(f'local model request failed: {exc}') from exc
