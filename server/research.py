"""Arachne 웹 리서치 클라이언트 — 전략 아이디어의 '사실 소스'를 모은다.

ArkInsight(투심보고서)의 src/research.py 와 같은 계약을 따른다:
  gather()          → Arachne 헤드리스 브라우저가 검색+본문추출 (LLM 없음)
  format_evidence() → [출처N] 번호매김 근거 블록 (LLM 프롬프트에 그대로 박는다)
그 분리(근거 수집 ↔ 합성)가 핵심이다. LLM 은 번호로만 인용하고, 파서가 번호를
URL 로 되돌린다 — 출처 없는 주장을 사후에 잡아낼 수 있다.

**전부 fail-open** 이다. Arachne 가 죽어 있어도 빈 근거를 돌려줄 뿐, 워커/GA 는
절대 멈추지 않는다 (근거 없는 아이디어는 LLM 사전지식만으로 만들어진다).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

LOG = logging.getLogger('genomicwqb.research')

RESEARCH_URL = os.environ.get('ARACHNE_RESEARCH_URL', 'http://127.0.0.1:8771')
# 출처 1건이 프롬프트에 들어갈 최대 길이. 너무 크면 LLM 컨텍스트를 근거가 다 먹는다.
SRC_CHARS = int(os.environ.get('IQC_RESEARCH_SRC_CHARS', '2400'))

# 하나의 요청을 여러 각도로 쪼개 검색한다 — 한 질의로는 논문·구현·한계가 같이 안 잡힌다.
# (ArkInsight 의 _DOSSIER_ASPECTS 와 같은 패턴, 퀀트 팩터 도메인에 맞춰 재작성)
ASPECTS: tuple[tuple[str, str], ...] = (
    ('학술 근거', '{q} cross-sectional equity factor academic research'),
    ('팩터 정의', '{q} factor construction formula definition quant'),
    ('실증 성과', '{q} factor backtest sharpe turnover empirical results'),
    ('한계·감쇠', '{q} factor decay crowding limitations criticism'),
    ('구현 사례', '{q} WorldQuant Brain alpha expression example'),
)


def _post(path: str, payload: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        f'{RESEARCH_URL}{path}',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def gather(query: str, *, k: int = 6, fetch_k: int = 4,
           timeout: float = 120.0) -> dict[str, Any]:
    """Arachne /research/gather — 검색 + 상위 N개 본문 추출을 한 번에.

    fail-open: 어떤 오류든 {} 를 반환한다(Arachne 다운/타임아웃/비정상 응답).
    """
    q = str(query or '').strip()
    if not q:
        return {}
    try:
        data = _post('/research/gather',
                     {'query': q, 'k': int(k), 'fetch_k': int(fetch_k)},
                     timeout=timeout)
        return data if isinstance(data, dict) else {}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        LOG.warning('arachne gather 실패 (무시하고 진행): %s: %s', type(e).__name__, e)
        return {}


def format_evidence(data: dict, start_idx: int = 1) -> tuple[str, list[dict], int]:
    """gather 산출물 → ([출처N] 블록, 출처목록, 다음 번호).

    번호는 **전역 연속**이다(여러 각도의 gather 를 이어붙여도 1..N 이 안 겹친다).
    본문을 못 받은 항목(extra)은 스니펫만 [참고N] 으로 싣는다 — 버리지 않는다.
    """
    idx = int(start_idx)
    lines: list[str] = []
    entries: list[dict] = []
    for r in (data or {}).get('results') or []:
        url = str(r.get('url') or '').strip()
        if not url:
            continue
        title = str(r.get('title') or '(제목 없음)').strip()
        body = str(r.get('content') or r.get('snippet') or '').strip()
        if not body:
            continue
        lines.append(f'[출처{idx}] {title}\nURL: {url}\n본문발췌: {body[:SRC_CHARS]}\n')
        entries.append({'n': idx, 'title': title, 'url': url, 'kind': 'source'})
        idx += 1
    for r in (data or {}).get('extra') or []:
        url = str(r.get('url') or '').strip()
        snip = str(r.get('snippet') or '').strip()
        if not url or not snip:
            continue
        title = str(r.get('title') or '(제목 없음)').strip()
        lines.append(f'[참고{idx}] {title}\nURL: {url}\n요약: {snip[:400]}\n')
        entries.append({'n': idx, 'title': title, 'url': url, 'kind': 'extra'})
        idx += 1
    return ('\n'.join(lines), entries, idx)


def build_research_evidence(query: str, *, k: int = 5, fetch_k: int = 3,
                            on_progress=None) -> tuple[str, list[dict]]:
    """요청 1건 → 여러 각도로 수집한 통합 근거 블록 + 출처 목록.

    on_progress(label, n_sources) 가 주어지면 각도마다 호출한다(UI 진행 표시).
    Arachne 가 전부 실패해도 ('', []) 를 돌려줄 뿐 예외를 던지지 않는다.
    """
    q = str(query or '').strip()
    if not q:
        return ('', [])
    blocks: list[str] = []
    all_entries: list[dict] = []
    seen_urls: set[str] = set()
    idx = 1
    for label, tmpl in ASPECTS:
        data = gather(tmpl.format(q=q), k=k, fetch_k=fetch_k)
        # 각도끼리 같은 URL 이 겹치면 번호만 늘고 정보는 안 는다 — 먼저 걸러낸다.
        if data:
            for key in ('results', 'extra'):
                data[key] = [r for r in (data.get(key) or [])
                             if str(r.get('url') or '') not in seen_urls]
                for r in data[key]:
                    seen_urls.add(str(r.get('url') or ''))
        block, entries, idx = format_evidence(data, start_idx=idx)
        if block:
            blocks.append(f'── {label} ──\n{block}')
            all_entries.extend(entries)
        if on_progress:
            try:
                on_progress(label, len(entries))
            except Exception:
                pass
    return ('\n'.join(blocks), all_entries)


def render_sources_section(entries: list[dict]) -> str:
    """[출처N] → URL 역매핑 섹션 (보고/감사용)."""
    if not entries:
        return ''
    lines = ['## 출처 (References)']
    for e in entries:
        tag = '출처' if e.get('kind') == 'source' else '참고'
        lines.append(f"[{tag}{e['n']}] {e.get('title','')} — {e.get('url','')}")
    return '\n'.join(lines)
