"""brain_rag — BRAIN 공식 문서·레퍼런스 위의 검색층.

왜 있나
-------
`docs/brain_learn/` 77편(약 43만자)과 `docs/brain_reference/`(연산자 82·데이터셋 857)를
모아뒀지만, 통째로 프롬프트에 실을 수는 없다. 필요한 대목만 꺼내 쓰려면 검색이 필요하다.

무엇이 달라지나 — 2026-07-21 발굴에서 결정적이었던 것들이 전부 **문서에 적혀 있었는데
우리가 모르고 역산하고 있었다**:
  - IS Ladder 문턱표 (역산 대신 문서에 표로 있음)
  - CHN 컷·Robust Universe 테스트 (아예 몰랐음)
  - Genius 타이브레이커 7종 (아예 몰랐음)
  - Multi-Simulation 이 순차 실행이라는 사실 (반대로 알고 있었음)
검색층이 있으면 다음부터는 묻고 답한다.

설계
----
- 임베딩은 **arcembed**(:8765 공용) 에 위임한다. 모델을 여기서 또 띄우지 않는다.
- 인덱스는 `data/brain_rag.json` 에 저장한다(재시작 무관, 문서가 바뀔 때만 재생성).
- **fail-soft**: arcembed 가 죽어 있으면 키워드 검색으로 자동 강등한다. 검색이 안 된다고
  워커가 멈추면 안 된다.
- 청크는 문단 경계로 자른다. 표(마크다운 파이프 행)는 쪼개면 의미가 죽으므로 붙여 둔다.
"""
from __future__ import annotations

import json
import math
import os
import re

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_THIS_DIR, '..'))
LEARN_DIR = os.path.join(ROOT, 'docs', 'brain_learn')
REF_DIR = os.path.join(ROOT, 'docs', 'brain_reference')
INDEX_PATH = os.path.join(ROOT, 'data', 'brain_rag.json')
ARCEMBED_URL = os.environ.get('ARCEMBED_URL', 'http://localhost:8765')

CHUNK_CHARS = 1400          # 임베딩 모델 max_length(4096 토큰) 대비 여유
CHUNK_OVERLAP = 200


def _split(text: str) -> list:
    """문단 경계로 자른다. 표는 통째로 유지한다(쪼개면 헤더를 잃어 의미가 죽는다)."""
    blocks, cur = [], []
    size = 0
    for para in re.split(r'\n\s*\n', text or ''):
        p = para.strip()
        if not p:
            continue
        if size + len(p) > CHUNK_CHARS and cur:
            blocks.append('\n\n'.join(cur))
            # 겹침 — 앞 블록 꼬리를 물고 시작해 경계에서 문맥이 끊기지 않게 한다.
            tail = '\n\n'.join(cur)[-CHUNK_OVERLAP:]
            cur, size = ([tail] if tail else []), len(tail)
        cur.append(p)
        size += len(p)
    if cur:
        blocks.append('\n\n'.join(cur))
    return [b for b in blocks if b.strip()]


def iter_documents() -> list:
    """색인 대상 원문 → [{'source','title','text'}…]"""
    docs = []
    if os.path.isdir(LEARN_DIR):
        for fn in sorted(os.listdir(LEARN_DIR)):
            if not fn.endswith('.md') or fn.startswith('INDEX'):
                continue
            path = os.path.join(LEARN_DIR, fn)
            try:
                body = open(path).read()
            except OSError:
                continue
            title = body.split('\n', 1)[0].lstrip('# ').strip() or fn
            docs.append({'source': f'brain_learn/{fn}', 'title': title, 'text': body})
    ref_md = os.path.join(REF_DIR, 'REFERENCE.md')
    if os.path.exists(ref_md):
        try:
            docs.append({'source': 'brain_reference/REFERENCE.md',
                         'title': 'BRAIN 레퍼런스(연산자·데이터셋)',
                         'text': open(ref_md).read()})
        except OSError:
            pass
    return docs


def _embed(texts: list, timeout: float = 240.0, query: bool = False) -> list | None:
    """arcembed 로 임베딩. 실패하면 None (호출부가 키워드 검색으로 강등).

    공용 라이브러리(`~/projects/lib/arcembed`)가 배치·재시도를 처리한다. 라이브러리가
    없으면 HTTP 로 직접 친다 — 계약은 POST /embed {inputs:[...]} → {embeddings:[...]}.
    질의는 `embed_query` 로 지시문(instruction)을 붙여야 문서-질의 비대칭이 맞는다.
    """
    if not texts:
        return []
    try:
        sys_path = os.path.expanduser('~/projects/lib')
        import sys as _sys
        if sys_path not in _sys.path:
            _sys.path.insert(0, sys_path)
        import arcembed
        if query:
            return [arcembed.embed_query(texts[0], timeout=timeout)]
        return arcembed.embed(texts, timeout=timeout)
    except Exception:
        pass
    try:
        import requests
        out: list = []
        for i in range(0, len(texts), 64):
            r = requests.post(f'{ARCEMBED_URL}/embed',
                              json={'inputs': texts[i:i + 64]}, timeout=timeout)
            if not r.ok:
                return None
            vecs = (r.json() or {}).get('embeddings')
            if not isinstance(vecs, list):
                return None
            out += vecs
        return out if len(out) == len(texts) else None
    except Exception:
        return None


def build_index(verbose: bool = True) -> dict:
    """문서 → 청크 → 임베딩 → data/brain_rag.json. 임베딩 실패해도 청크는 저장한다
    (키워드 검색만으로도 쓸모가 있다)."""
    chunks = []
    for d in iter_documents():
        for i, blk in enumerate(_split(d['text'])):
            chunks.append({'source': d['source'], 'title': d['title'],
                           'seq': i, 'text': blk})
    if verbose:
        print(f'문서 {len(iter_documents())}편 → 청크 {len(chunks)}개')

    vecs = None
    if chunks:
        vecs = _embed([c['text'] for c in chunks])
        if vecs is None and verbose:
            print('⚠ arcembed 응답 없음 — 임베딩 없이 저장(키워드 검색으로 동작)')
    # ⚠ arcembed 는 numpy 배열을 줄 수 있다 — `if vecs:` 로 진릿값을 물으면
    #   "truth value of an array is ambiguous" 로 죽는다. 길이로 판정한다.
    has_vecs = vecs is not None and len(vecs) == len(chunks)
    if has_vecs:
        for c, v in zip(chunks, vecs):
            c['vec'] = [float(x) for x in v]
    index = {'chunks': chunks, 'embedded': has_vecs,
             'dim': len(chunks[0]['vec']) if has_vecs and chunks else 0}
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    tmp = INDEX_PATH + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(index, fh, ensure_ascii=False)
    os.replace(tmp, INDEX_PATH)
    if verbose:
        mb = os.path.getsize(INDEX_PATH) / 1e6
        print(f'인덱스 저장 {INDEX_PATH} ({mb:.1f}MB, 임베딩={index["embedded"]})')
    return index


_CACHE = {'index': None}


def load_index(force: bool = False) -> dict | None:
    if _CACHE['index'] is not None and not force:
        return _CACHE['index']
    try:
        with open(INDEX_PATH) as fh:
            _CACHE['index'] = json.load(fh)
    except (OSError, ValueError):
        _CACHE['index'] = None
    return _CACHE['index']


def _cos(a, b) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return s / (na * nb)


def _keyword_score(query: str, text: str) -> float:
    toks = [t for t in re.split(r'\W+', query.lower()) if len(t) > 1]
    if not toks:
        return 0.0
    low = text.lower()
    return sum(low.count(t) for t in toks) / (len(toks) or 1)


def search(query: str, k: int = 5) -> list:
    """질의 → [{'source','title','text','score'}…]. 인덱스가 없으면 빈 리스트.

    임베딩이 있으면 코사인, 없으면(또는 arcembed 가 죽었으면) 키워드로 강등한다.
    """
    idx = load_index()
    if not idx or not idx.get('chunks'):
        return []
    chunks = idx['chunks']
    qv = _embed([query], timeout=30.0, query=True) if idx.get('embedded') else None
    if qv is not None and len(qv) == 1:
        q0 = [float(x) for x in qv[0]]
        scored = [(_cos(q0, c['vec']), c) for c in chunks if c.get('vec')]
    else:
        scored = [(_keyword_score(query, c['text']), c) for c in chunks]
    scored.sort(key=lambda t: -t[0])
    out = []
    for s, c in scored[:max(1, int(k))]:
        if s <= 0:
            continue
        out.append({'source': c['source'], 'title': c['title'],
                    'text': c['text'], 'score': round(float(s), 4)})
    return out
