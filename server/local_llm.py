"""HYFE_IQC 로컬 LLM seam — google.genai 드롭인 shim (외부 API 미사용).

Gemini(google-genai) 호출을 로컬 Ollama(OpenAI 호환 /v1/chat/completions)로 돌린다.
gemini_strategist.py / auth.py 의 import 두 줄만 이 모듈로 바꾸면:
  - genai.Client(...).models.generate_content(model, contents, config) → 로컬 모델 호출
  - prompt cache(client.caches.create) → NotImplementedError → 호출자가 비캐시 경로로 폴백
  - Google Search grounding(tools=...) / response_schema / ThinkingConfig → 무해한 no-op
모델명(gemini-*)은 무시하고 항상 LOCAL_LLM_MODEL 을 쓴다. JSON 산출은 프롬프트가 이미
"JSON 배열만" 을 강제하고 호출부 _parse_strategies 가 코드펜스/[...] 추출을 처리한다.
"""
import os

import requests

LOCAL_BASE_URL = os.environ.get('LOCAL_LLM_BASE_URL', 'http://127.0.0.1:11434/v1').rstrip('/')
LOCAL_MODEL = os.environ.get('LOCAL_LLM_MODEL', 'qwen3.6-35b-a3b-uncensored')
_TIMEOUT = int(os.environ.get('LOCAL_LLM_TIMEOUT', '600'))


class _Resp:
    """google.genai 응답 호환 — 호출부는 resp.text 만 읽는다."""
    def __init__(self, text):
        self.text = text or ''


class _Models:
    def generate_content(self, *, model=None, contents='', config=None):
        system = getattr(config, 'system_instruction', None) if config is not None else None
        temperature = getattr(config, 'temperature', 0.9) if config is not None else 0.9
        max_tokens = getattr(config, 'max_output_tokens', 4096) if config is not None else 4096
        messages = []
        if system:
            messages.append({'role': 'system', 'content': str(system)})
        messages.append({'role': 'user', 'content': contents if isinstance(contents, str) else str(contents)})
        body = {
            'model': LOCAL_MODEL,                 # gemini-* 무시, 로컬 모델 고정
            'messages': messages,
            'temperature': float(temperature) if temperature is not None else 0.9,
            'max_tokens': int(max_tokens) if max_tokens else 4096,
            'stream': False,
        }
        r = requests.post(LOCAL_BASE_URL + '/chat/completions', json=body, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        text = ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '') or ''
        return _Resp(text)


class _Caches:
    def create(self, *a, **k):
        # 로컬 LLM에는 Google prompt cache API가 없다. None을 반환하면
        # 호출부가 조용히 비캐시(전체 프롬프트) 경로를 사용한다.
        return None


class Client:
    def __init__(self, *a, **k):
        self.models = _Models()
        self.caches = _Caches()


# ── google.genai.types 드롭인 no-op 스텁 (모듈 로드시 스키마/툴 구성이 깨지지 않게) ──
class _Type:
    ARRAY = 'ARRAY'
    OBJECT = 'OBJECT'
    STRING = 'STRING'
    NUMBER = 'NUMBER'
    INTEGER = 'INTEGER'
    BOOLEAN = 'BOOLEAN'


class _Stub:
    """모든 키워드 인자를 속성으로 보관하는 범용 no-op (Config/Schema/Tool/Content 등)."""
    def __init__(self, *a, **k):
        self.__dict__.update(k)


class _TypesModule:
    Type = _Type
    GenerateContentConfig = _Stub
    Schema = _Stub
    CreateCachedContentConfig = _Stub
    Content = _Stub
    Part = _Stub
    Tool = _Stub
    GoogleSearch = _Stub
    ThinkingConfig = _Stub


types = _TypesModule()
