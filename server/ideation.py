"""Stage 1 — 수집된 근거로 전략 '가설'을 세운다 (LLM).

계약: 근거 블록([출처N])을 받아 가설 N개를 낸다. 각 가설은 반드시 인용번호를 갖는다 —
근거 없이 지어낸 가설을 사후에 걸러내기 위해서다(ArkInsight 의 사실성 가드와 같은 원리).

LLM 은 lib/arcllm 을 쓴다. server/local_llm.py 를 쓰지 않는 이유:
  로컬 Qwen3.6 은 **추론 모델**이라 max_tokens 가 작으면 thinking 만 뱉고 content 가
  빈 문자열로 온다. arcllm 은 기본 24000 + 빈응답 재시도가 들어 있고, local_llm 은
  기본 4096 에 재시도가 없다 (= 조용한 빈 응답).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

LOG = logging.getLogger('genomicwqb.ideation')

# 가설이 고를 수 있는 데이터 패밀리 — 유전체의 family 유전자와 1:1.
FAMILIES = ('pv', 'fundamental', 'analyst', 'option', 'news')

_SYSTEM = """너는 WorldQuant Brain 의 퀀트 알파 리서처다.
주어진 [수집 근거] 만을 사실 소스로 삼아, 횡단면(cross-sectional) 주식 알파 가설을 세운다.

규칙:
- 근거에 없는 수치·논문·주장을 지어내지 마라. 각 가설은 반드시 근거의 [출처N] 번호를 인용한다.
- 근거가 빈약하면 가설 수를 줄여라. 억지로 채우지 마라.
- 각 가설은 '어떤 데이터로 무엇을 재서 왜 초과수익이 나는가' 를 한 문장으로 말할 수 있어야 한다.
- 서로 다른 가설은 **서로 다른 데이터 패밀리나 다른 메커니즘**이어야 한다 (self-correlation 회피).
- family 는 반드시 다음 중 하나: pv(가격·거래량) | fundamental(재무) | analyst(애널리스트 추정)
  | option(내재변동성) | news(뉴스·심리)

출력은 **JSON 배열만**. 코드펜스·설명·머리말 금지.
각 원소:
{"title": "<40자 이내 한국어 제목>",
 "rationale": "<2~3문장. 경제적 근거 + 어떤 지표로 구현할지>",
 "citations": [1, 3],
 "family_hint": "fundamental"}"""


LLM_TIMEOUT_S = int(__import__('os').environ.get('IQC_IDEATE_TIMEOUT_S', '900'))
"""LLM 호출 1건의 상한(초). arcllm 기본은 3600 이고 재시도가 2회라 **최대 3시간**을
한 스레드가 점유할 수 있다 — 로컬 GPU 는 다른 프로젝트와 공유하므로 상한을 둔다.
15분이면 로컬 추론모델이 가설/후보를 내기에 충분하고(실측: 가설 2개 112초),
넘기면 fail-open 으로 빈 문자열 → GA 는 평소대로 계속 돈다."""


def _llm(messages: list[dict], *, max_tokens: int = 24000,
         temperature: float = 0.7, timeout: int | None = None,
         think: bool = True) -> str:
    """arcllm 호출. 실패/타임아웃이면 빈 문자열 (호출자가 fail-open).

    think=False 면 추론을 끈다 (QuantInSight 와 같은 규약:
    `chat_template_kwargs.enable_thinking`). 2026-07-27 사장 지시 —
    **판단은 1회만 추론으로, 그 뒤 '식 쓰기' 반복은 추론 없이** 돌린다.
    추론 반복은 느리고(수 분/회) 빈 응답·폭주 위험이 있다.
    """
    try:
        import arcllm
    except ImportError:
        LOG.warning('arcllm 미설치 — 아이디어 생성 건너뜀')
        return ''
    try:
        if think:
            return arcllm.chat(messages, max_tokens=max_tokens,
                               temperature=temperature,
                               timeout=int(timeout or LLM_TIMEOUT_S))
        return _chat_no_think(messages, max_tokens=max_tokens,
                              temperature=temperature,
                              timeout=int(timeout or LLM_TIMEOUT_S))
    except Exception as e:
        LOG.warning('LLM 호출 실패: %s: %s', type(e).__name__, e)
        return ''


def _chat_no_think(messages: list[dict], *, max_tokens: int, temperature: float,
                   timeout: int) -> str:
    """추론 OFF 호출 — arcllm 이 이 노브를 노출하지 않아 여기서 직접 POST 한다."""
    import json as _json
    import os as _os
    import urllib.request as _u
    base = (_os.environ.get('LOCAL_LLM_BASE_URL')
            or 'http://127.0.0.1:11434/v1').rstrip('/')
    model = _os.environ.get('LOCAL_LLM_MODEL', 'qwen3.6-35b-a3b-uncensored')
    body = _json.dumps({
        'model': model, 'messages': messages, 'stream': False,
        'temperature': temperature, 'max_tokens': max_tokens,
        'chat_template_kwargs': {'enable_thinking': False},
    }).encode()
    req = _u.Request(base + '/chat/completions', data=body,
                     headers={'Content-Type': 'application/json'})
    with _u.urlopen(req, timeout=timeout) as r:
        data = _json.loads(r.read())
    return ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '') or ''


_FENCE_RX = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*$', re.MULTILINE)


def _balanced_slice(s: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """s[start] 의 괄호와 짝이 맞는 지점까지 잘라낸다 (문자열 리터럴 내부는 무시)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def parse_json_array(text: str) -> list[dict]:
    """LLM 응답에서 JSON 객체 리스트를 뽑는다. 코드펜스·머리말·꼬리말에 관대하다.

    로컬 추론모델은 '순수 JSON 배열' 을 요구해도 (a) 앞뒤에 말을 붙이거나,
    (b) 배열 대신 객체 하나만 주거나, (c) 객체를 줄줄이 나열하는(배열 없이) 일이 잦다.
    셋 다 받아준다 — 안 그러면 멀쩡한 후보를 파싱 실패로 버린다.
    실패하면 [] — 절대 raise 하지 않는다.
    """
    s = (text or '').strip()
    if not s:
        return []
    s = _FENCE_RX.sub('', s).strip()

    # (a) 정상 배열
    start = s.find('[')
    if start >= 0:
        chunk = _balanced_slice(s, start, '[', ']')
        if chunk:
            try:
                obj = json.loads(chunk)
                if isinstance(obj, list):
                    out = [x for x in obj if isinstance(x, dict)]
                    if out:
                        return out
            except ValueError:
                pass

    # (b)(c) 배열이 없거나 깨졌다 → 최상위 객체들을 하나씩 긁어모은다.
    out: list[dict] = []
    i = 0
    while True:
        j = s.find('{', i)
        if j < 0:
            break
        chunk = _balanced_slice(s, j, '{', '}')
        if not chunk:
            break
        try:
            o = json.loads(chunk)
            if isinstance(o, dict):
                out.append(o)
        except ValueError:
            pass
        i = j + len(chunk)
    return out


def propose_hypotheses(query: str, evidence: str, *, n: int = 4) -> list[dict[str, Any]]:
    """요청 + 근거 → 전략 가설 리스트.

    근거가 비어 있어도(Arachne 실패) 동작한다 — 그 경우 LLM 의 사전지식만으로 세우되,
    citations 는 빈 배열이 된다(대시보드가 '근거 없음' 으로 표시).
    반환: [{title, rationale, citations:[int], family_hint}]
    """
    q = str(query or '').strip()
    if not q:
        return []
    ev = (evidence or '').strip()
    if ev:
        user = (f'[사용자 요청]\n{q}\n\n[수집 근거]\n{ev}\n\n'
                f'위 근거에 기반해 서로 다른 메커니즘의 알파 가설 {n}개를 JSON 배열로 내라.')
    else:
        user = (f'[사용자 요청]\n{q}\n\n[수집 근거]\n(웹 리서치 실패 — 근거 없음)\n\n'
                f'근거를 못 모았다. 네 사전지식만으로 알파 가설 {n}개를 JSON 배열로 내되, '
                f'citations 는 반드시 빈 배열 [] 로 두어라.')
    raw = _llm([{'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': user}])
    out: list[dict[str, Any]] = []
    for h in parse_json_array(raw)[:n]:
        title = str(h.get('title') or '').strip()
        if not title:
            continue
        fam = str(h.get('family_hint') or '').strip().lower()
        cits = [int(c) for c in (h.get('citations') or [])
                if str(c).lstrip('-').isdigit()]
        out.append({
            'title': title[:200],
            'rationale': str(h.get('rationale') or '').strip()[:2000],
            'citations': cits if ev else [],
            'family_hint': fam if fam in FAMILIES else '',
        })
    if not out:
        LOG.warning('가설 파싱 실패 — LLM 응답 %d자', len(raw))
    return out
