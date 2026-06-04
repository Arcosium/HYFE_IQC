"""알파 시뮬 settings 의 정규화 + fingerprint.

캐시 키를 (code_hash, settings_fingerprint) 로 만들기 위한 순수 모듈.
'요청 settings' 가 아니라 '실제 적용될 effective settings'(WQB 기본값 채움) 위에서
계산하므로 {universe:TOP3000} 와 {} 가 같은 fingerprint 를 갖는다.
delay 는 라운드의 forced_delay 에서 주입한다 (Gemini settings 의 delay 는 무시).
"""
from __future__ import annotations

import hashlib

# Gemini 가 키를 생략하면 이 값으로 시뮬된다 (WQB UI 기본값).
WQB_DEFAULTS = {
    'region': 'USA',
    'universe': 'TOP3000',
    'neutralization': 'INDUSTRY',
    'decay': '0',
    'truncation': '0.01',
    'pasteurization': 'ON',
    'nan_handling': 'OFF',
}
# fingerprint 직렬화 순서 (고정).
_FP_KEYS = ('region', 'universe', 'delay', 'neutralization', 'decay',
            'truncation', 'pasteurization', 'nan_handling')


def _norm_val(v) -> str:
    """숫자는 표기 통일(0.010->0.01, 4.0->4), 그 외 문자열은 대문자화."""
    s = str(v).strip()
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s.upper()
    if f == int(f):
        return str(int(f))
    return f'{f:.6f}'.rstrip('0').rstrip('.')


def effective_settings(partial, forced_delay) -> dict:
    """WQB_DEFAULTS 위에 partial 을 덮고 delay 는 forced_delay 로 강제. 값 정규화."""
    eff = dict(WQB_DEFAULTS)
    if isinstance(partial, dict):
        for k, v in partial.items():
            kk = str(k).strip().lower()
            if kk in WQB_DEFAULTS and v is not None and str(v).strip():
                eff[kk] = str(v).strip()
    eff['delay'] = str(forced_delay)
    return {k: _norm_val(val) for k, val in eff.items()}


def settings_fingerprint(eff: dict) -> str:
    raw = '|'.join(f'{k}={eff.get(k, "")}' for k in _FP_KEYS)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
