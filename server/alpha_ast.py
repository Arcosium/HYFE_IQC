"""Tolerant recursive-descent parser for WQB alpha expressions.

Design philosophy: ALLOW on any parse uncertainty/failure. Unknown identifiers
are NOT errors — our catalog is partial. Only flag provably-broken structure.

Public API
----------
tokenize(code)        -> list of token dicts
parse(code)           -> Node dict tree, or None on hard failure
operators_used(code)  -> set[str] — operator names (catalog.is_operator) used as calls
fields_used(code)     -> set[str] — NAME tokens used as operands, NOT operators, len>=2
max_depth(code)       -> int — max parenthesis/call nesting depth
outermost_operator(code) -> str | None — top-level op name if whole expr is one call
validate(code)        -> list[str] — provable structural issues ONLY; [] on parse failure

Node shapes
-----------
call:   {'type':'call', 'name':<lower_str>, 'args':[Node,...]}
name:   {'type':'name', 'value':<lower_str>}
num:    {'type':'num',  'value':<str>}
str:    {'type':'str'}
group:  {'type':'group', 'children':[Node,...]}
unary:  {'type':'unary', 'op':'-', 'operand':Node}
"""
from __future__ import annotations

import re
from typing import Iterator

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOK_RX = re.compile(
    r'(?P<STR>"[^"]*")'           # double-quoted string (content ignored)
    r'|(?P<NAME>[A-Za-z_]\w*)'    # identifier
    r'|(?P<NUM>\d+\.?\d*|\.\d+)'  # number (int or decimal; NOT sci-notation)
    r'|(?P<LPAREN>\()'
    r'|(?P<RPAREN>\))'
    r'|(?P<COMMA>,)'
    r'|(?P<SEMI>;)'
    # '?' 와 ':' 는 FASTEXPR 삼항연산자(cond ? a : b). 여기 없으면 토크나이저가 그 두
    # 문자를 **조용히 버려서** `x>1?a:b` 가 [x,1,a,b] 로 납작해진다 — 파스 트리가 원식과
    # 달라진다(2026-07-14, regime 유전자 도입으로 삼항이 정식 산출물이 되며 발견).
    r'|(?P<OP>[+\-*/^<>=&|!%?:])'
    r'|(?P<WS>\s+)'               # whitespace — consumed but not emitted
)


def tokenize(code: str) -> list:
    """Return list of token dicts with keys 'kind' and 'value'. Tolerant:
    unrecognised chars produce no token (silently skipped)."""
    if not isinstance(code, str):
        return []
    tokens: list[dict] = []
    for m in _TOK_RX.finditer(code):
        kind = m.lastgroup
        if kind == 'WS':
            continue
        tokens.append({'kind': kind, 'value': m.group()})
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser:
    """Recursive-descent parser. Returns Node dicts. Never raises — returns
    None on hard failure, builds best-effort partial tree otherwise."""

    def __init__(self, tokens: list):
        self._toks = tokens
        self._pos = 0

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _peek(self) -> dict | None:
        if self._pos < len(self._toks):
            return self._toks[self._pos]
        return None

    def _consume(self) -> dict | None:
        tok = self._peek()
        if tok is not None:
            self._pos += 1
        return tok

    def _at_kind(self, kind: str) -> bool:
        t = self._peek()
        return t is not None and t['kind'] == kind

    # ------------------------------------------------------------------
    # grammar: statement_list → statement (SEMI statement)*
    #          statement → expr
    #          expr → atom (OP atom)*    [flat arith chain → group node]
    #          atom → NAME LPAREN arglist RPAREN   [call]
    #               | NAME                          [identifier]
    #               | NUM                           [number]
    #               | STR                           [string literal]
    #               | LPAREN expr RPAREN            [parenthesised expr]
    # ------------------------------------------------------------------

    def parse_all(self):
        """Parse a full code string (may contain semicolons). Returns a single
        Node — if there is exactly one statement, return it directly; otherwise
        wrap in a group."""
        stmts = []
        node = self._parse_expr()
        if node is not None:
            stmts.append(node)
        while self._at_kind('SEMI'):
            self._consume()          # eat ';'
            node = self._parse_expr()
            if node is not None:
                stmts.append(node)
        if not stmts:
            return None
        if len(stmts) == 1:
            return stmts[0]
        return {'type': 'group', 'children': stmts}

    def _parse_expr(self) -> dict | None:
        """Parse an expression: one or more atoms joined by binary operators.
        If it's a single atom, return it directly. If there are operators,
        wrap everything in a group so we don't confuse it with a plain call."""
        first = self._parse_atom()
        if first is None:
            return None
        parts = [first]
        while self._at_kind('OP'):
            self._consume()           # consume the operator symbol
            rhs = self._parse_atom()
            if rhs is not None:
                parts.append(rhs)
        if len(parts) == 1:
            return parts[0]
        return {'type': 'group', 'children': parts}

    def _parse_atom(self) -> dict | None:
        t = self._peek()
        if t is None:
            return None

        # parenthesised group: ( expr )
        if t['kind'] == 'LPAREN':
            self._consume()
            inner = self._parse_expr()
            if self._at_kind('RPAREN'):
                self._consume()
            # return the inner node directly (or a group marker)
            if inner is None:
                return {'type': 'group', 'children': []}
            return inner

        # number
        if t['kind'] == 'NUM':
            self._consume()
            return {'type': 'num', 'value': t['value']}

        # string literal
        if t['kind'] == 'STR':
            self._consume()
            return {'type': 'str'}

        # identifier — might be a function call
        if t['kind'] == 'NAME':
            self._consume()
            name_lower = t['value'].lower()
            # look-ahead: followed by '(' → function call
            if self._at_kind('LPAREN'):
                self._consume()   # eat '('
                args = self._parse_arglist()
                if self._at_kind('RPAREN'):
                    self._consume()   # eat ')'
                return {'type': 'call', 'name': name_lower, 'args': args}
            # plain identifier
            return {'type': 'name', 'value': name_lower}

        # unary +/- : consume the sign, parse the following atom
        if t['kind'] == 'OP' and t['value'] in ('-', '+'):
            self._consume()
            operand = self._parse_atom()
            if operand is None:
                return None
            if t['value'] == '+':
                return operand
            return {'type': 'unary', 'op': '-', 'operand': operand}

        # anything else (stray OP, COMMA, RPAREN at wrong level) — skip
        self._consume()
        return None

    def _parse_arglist(self) -> list:
        """Parse comma-separated list of expressions until ')' or end of tokens."""
        args: list = []
        # empty arglist
        if self._at_kind('RPAREN') or self._peek() is None:
            return args
        first = self._parse_expr()
        if first is not None:
            args.append(first)
        while self._at_kind('COMMA'):
            self._consume()   # eat ','
            arg = self._parse_expr()
            if arg is not None:
                args.append(arg)
        return args


def parse(code: str):
    """Parse *code* into a Node tree. Returns None on hard failure or empty
    input. Tolerant: never raises; best-effort on malformed input."""
    if not isinstance(code, str):
        return None
    code = code.strip()
    if not code:
        return None
    try:
        tokens = tokenize(code)
        if not tokens:
            return None
        p = _Parser(tokens)
        return p.parse_all()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Feature extractors — walk the Node tree
# ---------------------------------------------------------------------------

def _walk(node) -> Iterator[dict]:
    """DFS walk over all nodes in the tree."""
    if not isinstance(node, dict):
        return
    yield node
    for child in _children_of(node):
        yield from _walk(child)


def _children_of(node: dict) -> list:
    if node.get('type') == 'call':
        return node.get('args', [])
    if node.get('type') == 'group':
        return node.get('children', [])
    if node.get('type') == 'unary':
        op = node.get('operand')
        return [op] if isinstance(op, dict) else []
    return []


def operators_used(code: str) -> set:
    """Return set of operator names (according to catalog.is_operator) that
    appear as function calls in *code*. Tolerant: returns {} on failure."""
    try:
        from . import operator_catalog
    except ImportError:
        try:
            import operator_catalog  # type: ignore
        except ImportError:
            return set()
    try:
        tree = parse(code)
        if tree is None:
            return set()
        result: set[str] = set()
        for node in _walk(tree):
            if node.get('type') == 'call':
                n = node['name']
                if operator_catalog.is_operator(n):
                    result.add(n)
        return result
    except Exception:
        return set()


def call_arities(code: str) -> list:
    """Return [(op_name, arg_count), ...] for every function-call node.
    arg_count = number of top-level args to that call. Tolerant: [] on failure.
    #2 프리플라이트 arity 검증(operator_catalog.arity 와 대조)용."""
    try:
        tree = parse(code)
        if tree is None:
            return []
        out = []
        for node in _walk(tree):
            if isinstance(node, dict) and node.get('type') == 'call':
                out.append((node.get('name'), len(node.get('args') or [])))
        return out
    except Exception:
        return []


def fields_used(code: str) -> set:
    """Return set of NAME tokens used as operands that are NOT operators and
    whose name has length >= 2. Tolerant: returns {} on failure.

    명명인자 키(`std=4`, `filter=True`, `hump=0.03`)와 그룹명(`sector` 등)은
    **데이터필드가 아니다**. 파서는 `std=4` 를 name('std') + OP('=') + number 로
    쪼개므로 그대로 두면 'std' 가 필드로 잡히고, presim_gate 의 팔레트 검사가
    "unknown datafield: std" 로 알파를 통째로 드롭한다. 이 제외 규칙은 원래
    apply_field_hygiene 에만 있었는데(=아래 _HYGIENE_* 상수), 필드 추출 자체의
    성질이므로 여기로 내린다 — 두 호출자가 같은 진실을 쓴다.
    """
    try:
        from . import operator_catalog
    except ImportError:
        try:
            import operator_catalog  # type: ignore
        except ImportError:
            return set()
    try:
        tree = parse(code)
        if tree is None:
            return set()
        exclude = (set(_HYGIENE_ASSIGN_RX.findall(code))
                   | _HYGIENE_GROUP_NAMES | _HYGIENE_LITERALS)
        result: set[str] = set()
        for node in _walk(tree):
            if node.get('type') == 'name':
                n = node['value']
                if len(n) >= 2 and n not in exclude \
                        and not operator_catalog.is_operator(n):
                    result.add(n)
        return result
    except Exception:
        return set()


def max_depth(code: str) -> int:
    """Return maximum call/parenthesis nesting depth. 0 for a bare token.
    Tolerant: returns 0 on failure."""
    try:
        tree = parse(code)
        if tree is None:
            return 0
        return _node_depth(tree)
    except Exception:
        return 0


def _node_depth(node) -> int:
    if not isinstance(node, dict):
        return 0
    children = _children_of(node)
    if not children:
        return 1 if node.get('type') == 'call' else 0
    child_max = max((_node_depth(c) for c in children), default=0)
    if node.get('type') == 'call':
        return 1 + child_max
    return child_max


def outermost_operator(code: str):
    """Return the top-level operator name if the whole expression is a single
    call to a known operator, else None. Tolerant: returns None on failure."""
    try:
        from . import operator_catalog
    except ImportError:
        try:
            import operator_catalog  # type: ignore
        except ImportError:
            return None
    try:
        tree = parse(code)
        if tree is None:
            return None
        if tree.get('type') == 'call' and operator_catalog.is_operator(tree['name']):
            return tree['name']
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scoped raw-vector check
# ---------------------------------------------------------------------------

def _collect_field_enclosing_op(node, parent_call_name: str | None, result: list):
    """Walk tree and for each 'name' leaf, record (field_lower, enclosing_op_name).
    enclosing_op_name is the .name of the nearest ancestor 'call' node (or None
    if there is no call ancestor — e.g. bare top-level identifiers)."""
    if not isinstance(node, dict):
        return
    ntype = node.get('type')
    if ntype == 'name':
        result.append((node['value'], parent_call_name))
        return
    if ntype == 'call':
        call_name = node['name']
        for arg in node.get('args', []):
            _collect_field_enclosing_op(arg, call_name, result)
        return
    if ntype == 'group':
        for child in node.get('children', []):
            _collect_field_enclosing_op(child, parent_call_name, result)
        return
    if ntype == 'unary':
        _collect_field_enclosing_op(node.get('operand'), parent_call_name, result)
        return
    # num, str — no names inside


def validate(code: str) -> list[str]:
    """Return list of provable structural issues. Returns [] (allow) on any
    parse failure or ambiguity.

    Checks:
    1. Unbalanced parentheses (string-aware).
    2. Scoped raw-vector: a vector-type field used directly inside a call
       whose name does NOT start with 'vec_' (per-occurrence check).
    """
    if not isinstance(code, str):
        return []
    issues: list[str] = []

    # --- 1. Balanced parens (string-aware) ---
    depth = 0
    in_str = False
    unbalanced = False
    for ch in code:
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                unbalanced = True
                break
    if not unbalanced and depth != 0:
        unbalanced = True
    if unbalanced:
        issues.append('unbalanced parentheses')

    # --- 2. Scoped raw-vector check ---
    try:
        from . import alpha_lint as _lint
    except ImportError:
        try:
            import alpha_lint as _lint  # type: ignore
        except ImportError:
            _lint = None  # type: ignore

    vec_fields: set[str] = set()
    if _lint is not None:
        try:
            vec_fields = _lint.vector_datafields()
        except Exception:
            pass

    if vec_fields:
        try:
            tree = parse(code)
            if tree is not None:
                field_op_pairs: list[tuple[str, str | None]] = []
                _collect_field_enclosing_op(tree, None, field_op_pairs)
                raw_vec: list[str] = []
                seen: set[str] = set()
                for (field, enclosing_op) in field_op_pairs:
                    if field not in vec_fields:
                        continue
                    # It's a vector field. Check enclosing op.
                    if enclosing_op is None or not enclosing_op.startswith('vec_'):
                        if field not in seen:
                            raw_vec.append(field)
                            seen.add(field)
                if raw_vec:
                    issues.append(
                        'raw vector-type datafield (vec_* 로 감싸야 함): '
                        + ', '.join(raw_vec[:6])
                    )
        except Exception:
            pass  # parse failure → allow

    return issues


# ---------------------------------------------------------------------------
# Complexity metrics  (Task 1)
# ---------------------------------------------------------------------------

def symbol_length(code) -> int:
    """Character length of the expression (complexity proxy). 0 on non-str."""
    return len(code) if isinstance(code, str) else 0


def base_feature_count(code) -> int:
    """Number of DISTINCT base datafields used (== len(fields_used)). 0 on failure."""
    try:
        return len(fields_used(code))
    except Exception:
        return 0


def free_const_ratio(code) -> float:
    """Fraction of tree nodes that are numeric literals — over-parameterisation
    proxy. 0.0 on failure / empty tree."""
    try:
        tree = parse(code)
        if tree is None:
            return 0.0
        nodes = list(_walk(tree))
        if not nodes:
            return 0.0
        nums = sum(1 for n in nodes if n.get('type') == 'num')
        return nums / len(nodes)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Structural overlap / largest-common-subtree  (Task 2)
# ---------------------------------------------------------------------------

def _shape_key(node) -> tuple:
    """Structural signature ignoring leaf values, keeping type+name+arity. Leaves
    (name/num/str) collapse to ('LEAF',) so different fields/windows match."""
    if not isinstance(node, dict):
        return ('LEAF',)
    t = node.get('type')
    if t == 'call':
        return ('call', node.get('name'), len(node.get('args', [])))
    if t == 'unary':
        return ('unary', node.get('op'))
    if t == 'group':
        return ('group', len(node.get('children', [])))
    return ('LEAF',)


def _subtree_size(node) -> int:
    return sum(1 for _ in _walk(node))


def _identical(a, b) -> bool:
    """Recursive structural equality (type+name+arity, children compared in order)."""
    if _shape_key(a) != _shape_key(b):
        return False
    ca, cb = _children_of(a), _children_of(b)
    if len(ca) != len(cb):
        return False
    return all(_identical(x, y) for x, y in zip(ca, cb))


def largest_common_subtree(code_a, code_b) -> int:
    """Size (node count) of the largest structurally-identical subtree shared by the
    two expressions. 0 on parse failure. Pre-sim decorrelation signal. O(Na*Nb)."""
    try:
        ta, tb = parse(code_a), parse(code_b)
        if ta is None or tb is None:
            return 0
        nodes_b = list(_walk(tb))
        best = 0
        for na in _walk(ta):
            sk = _shape_key(na)
            for nb in nodes_b:
                if sk == _shape_key(nb) and _identical(na, nb):
                    sz = _subtree_size(na)
                    if sz > best:
                        best = sz
        return best
    except Exception:
        return 0


def structural_overlap(code, others):
    """Return (max_overlap_size, index_of_worst) vs a list of existing code strings.
    (0, -1) if others empty or on failure."""
    try:
        best, best_i = 0, -1
        for i, o in enumerate(others or []):
            s = largest_common_subtree(code, o)
            if s > best:
                best, best_i = s, i
        return best, best_i
    except Exception:
        return 0, -1


# ---------------------------------------------------------------------------
# Field hygiene — deterministic winsorize(ts_backfill(F,120),std=4) auto-wrap
# ---------------------------------------------------------------------------
# LLM 비순응 보완(참조: worldquant-miner machine_lib.process_datafields): base 데이터필드를
# 코드 레벨에서 결정론적으로 위생 래핑해 Sharpe~0.2(결측갭+이상치 횡단면 지배)를 차단한다.
_HYGIENE_GROUP_NAMES = {'sector', 'industry', 'subindustry', 'market', 'country'}
_HYGIENE_LITERALS = {'true', 'false', 'nan'}
_HYGIENE_ASSIGN_RX = re.compile(r'(\w+)\s*=(?!=)')   # sig_a= , std= , filter=  (== 은 제외)


def apply_field_hygiene(code: str, *, backfill: int = 120, std: int = 4) -> str:
    """base 데이터필드를 winsorize(ts_backfill(F, backfill), std=std) 로 자동 래핑.

    제외: sig_ 중간변수·named-arg key(X=...)·group 명(sector 등)·리터럴·이미 ts_backfill 로
    감싼 필드. vec_ 벡터 알파는 그대로 둔다(vec_* 별도 처리 필요). 절대 raise 안 함."""
    try:
        if not code or 'vec_' in code:
            return code
        fields = fields_used(code)
        if not fields:
            return code
        assigned = set(_HYGIENE_ASSIGN_RX.findall(code))
        exclude = assigned | _HYGIENE_GROUP_NAMES | _HYGIENE_LITERALS
        # 긴 이름부터 — 짧은 필드가 긴 필드의 부분문자열일 때 오치환 방지
        targets = sorted((f for f in fields if f not in exclude), key=len, reverse=True)
        for f in targets:
            wrapped = f'winsorize(ts_backfill({f}, {backfill}), std={std})'
            # 단어경계 + 이미 ts_backfill( 안에 있는 필드는 건너뛴다(멱등)
            pat = r'(?<!ts_backfill\()(?<!\w)' + re.escape(f) + r'(?!\w)'
            code = re.sub(pat, wrapped, code)
        return code
    except Exception:
        return code
