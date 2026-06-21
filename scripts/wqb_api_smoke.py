"""하우스 RC 자격증명으로 WQB 공식 API 계약을 라이브 확인.

사용:
    WQB_EMAIL=you@example.com WQB_PASSWORD='***' python3.11 scripts/wqb_api_smoke.py

어떤 데이터도 영구 변경하지 않는다(시뮬은 읽기성 백테스트, 알파 제출 안 함).
출력으로 §10 미확정 스키마(시뮬 settings 바디·correlation 엔드포인트·data-fields
페이지네이션)를 확정한다. 차이가 있으면 server/wqb_api.py 의
_full_settings / _extract_max_correlation 및 server/wqb_data_service.map_datafields 를 조정한다.
"""
import json
import os
import sys
import time

import requests
from requests.auth import HTTPBasicAuth

BASE = 'https://api.worldquantbrain.com'

try:
    EMAIL = os.environ['WQB_EMAIL']
    PW = os.environ['WQB_PASSWORD']
except KeyError:
    sys.exit('WQB_EMAIL / WQB_PASSWORD 환경변수를 설정하세요.')

s = requests.Session()
s.auth = HTTPBasicAuth(EMAIL, PW)

r = s.post(BASE + '/authentication')
print('AUTH', r.status_code, 'WWW-Authenticate=', r.headers.get('WWW-Authenticate'))
print('AUTH-BODY', r.text[:400])
if r.status_code not in (200, 201):
    sys.exit('인증 실패 — 이후 단계 생략.')

# 최소 시뮬 — 보편 PV 식.
body = {
    'type': 'REGULAR',
    'settings': {
        'instrumentType': 'EQUITY', 'region': 'USA', 'universe': 'TOP3000',
        'delay': 1, 'decay': 0, 'neutralization': 'INDUSTRY',
        'truncation': 0.08, 'pasteurization': 'ON', 'unitHandling': 'VERIFY',
        'nanHandling': 'OFF', 'language': 'FASTEXPR', 'visualization': False,
    },
    'regular': 'rank(close)',
}
r = s.post(BASE + '/simulations', json=body)
print('SIM', r.status_code, 'Location=', r.headers.get('Location'))
loc = r.headers.get('Location')
alpha_id = None
if loc:
    for _ in range(120):
        pr = s.get(loc)
        j = pr.json()
        print('POLL', pr.status_code, 'progress=', j.get('progress'),
              'status=', j.get('status'), 'alpha=', j.get('alpha'))
        if j.get('status') in ('COMPLETE', 'ERROR', 'FAIL', 'WARNING'):
            alpha_id = j.get('alpha')
            break
        time.sleep(5)

if alpha_id:
    a = s.get(BASE + f'/alphas/{alpha_id}')
    print('ALPHA keys=', list(a.json().keys()))
    print('IS=', json.dumps(a.json().get('is'), indent=2)[:1200])
    c = s.get(BASE + f'/alphas/{alpha_id}/correlations/self')
    print('CORR', c.status_code, c.text[:600])

df = s.get(BASE + '/data-fields',
           params={'region': 'USA', 'delay': 1, 'universe': 'TOP3000', 'limit': 3, 'offset': 0})
print('DATAFIELDS', df.status_code, json.dumps(df.json(), indent=2)[:1000])

op = s.get(BASE + '/operators')
print('OPERATORS', op.status_code, str(op.json())[:400])
