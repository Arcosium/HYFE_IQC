#!/usr/bin/env python3
"""표준(비-RC) 계정이 WQB REST API 로 인증·시뮬할 수 있는지 실증한다.

이것은 **Playwright 경로 제거의 하드 게이트**다. 이 스크립트가 성공 트랜스크립트를
남기기 전까지 브라우저 시뮬 코드를 지우면 안 된다 — 지웠는데 표준 계정이 API 를 못
쓰면 그 계정은 시뮬 수단을 완전히 잃는다.

WQB 호출은 최대 5회(auth·submit·poll 몇 번·get). 결과는 data/verify_std_api.json 에
남긴다. 표준 계정 자격증명은 인자 또는 STD_EMAIL/STD_PASSWORD 환경변수로 준다.

사용:
    python3 scripts/verify_std_api.py <email> <password>
    STD_EMAIL=... STD_PASSWORD=... python3 scripts/verify_std_api.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import auth, wqb_api  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'data', 'verify_std_api.json')

# 가장 단순하고 확실히 컴파일되는 알파 — 능력 확인이 목적이지 성적이 아니다.
PROBE_ALPHA = 'rank(close)'
PROBE_SETTINGS = {
    'instrumentType': 'EQUITY', 'region': 'USA', 'universe': 'TOP3000',
    'delay': 1, 'decay': 0, 'neutralization': 'INDUSTRY', 'truncation': 0.08,
    'pasteurization': 'ON', 'unitHandling': 'VERIFY', 'nanHandling': 'OFF',
    'language': 'FASTEXPR', 'visualization': False,
}


def main() -> int:
    email = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('STD_EMAIL', '')).strip()
    password = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get('STD_PASSWORD', '')).strip()
    if not (email and password):
        print('사용법: verify_std_api.py <email> <password>  '
              '(또는 STD_EMAIL/STD_PASSWORD)')
        return 2

    transcript = {'email_sha_hint': email[:3] + '***', 'steps': [], 'ok': False,
                  'ts': time.strftime('%Y-%m-%d %H:%M:%S')}

    def rec(step, **kw):
        transcript['steps'].append({'step': step, **kw})
        print(f'[{step}] ' + ' '.join(f'{k}={v}' for k, v in kw.items()))

    # 1) 능력 탐침
    probe = auth.probe_wqb_backend(email, password)
    rec('probe', backend=probe.get('backend'), reason=probe.get('reason'))
    if probe.get('backend') != 'api':
        if probe.get('reason') == 'wqb_persona_required':
            rec('note', msg='이 계정도 persona 인증이 필요합니다. 대시보드에서 완료 후 다시 실행하세요.',
                persona_url=probe.get('persona_url'))
        transcript['conclusion'] = ('표준 계정이 API 로 인증되지 않음 — Playwright 경로를 '
                                    '유지해야 합니다.')
        _save(transcript)
        return 1

    # 2) 실제 시뮬 1건 (submit → poll → fetch)
    client = wqb_api.WqbApiClient(email, password)
    if not client.authenticate():
        rec('authenticate', ok=False, persona=client.persona_required)
        transcript['conclusion'] = '인증 실패'
        _save(transcript)
        return 1
    rec('authenticate', ok=True, amr=client.auth_methods(),
        expiry_h=round((client.seconds_to_expiry() or 0) / 3600, 2))

    try:
        result = client.run_simulation(PROBE_ALPHA, PROBE_SETTINGS) \
            if hasattr(client, 'run_simulation') else None
    except Exception as e:
        result = None
        rec('simulate', ok=False, error=f'{type(e).__name__}: {e}')

    if result is None:
        # run_simulation 헬퍼가 없으면 백엔드로 배치 1건.
        from server.wqb_backend import ApiBackend
        be = ApiBackend(email, password)
        try:
            rows = be.simulate_batch(
                [{'idx': 1, 'code': PROBE_ALPHA, 'desc': 'probe',
                  'settings': {k: str(v) for k, v in PROBE_SETTINGS.items()}}],
                wqb_username=email, wqb_password=password, forced_delay='1')
            r0 = rows[0] if rows else {}
            ok = not (r0.get('error_text') or '').strip()
            rec('simulate', ok=ok, pass_count=r0.get('pass_count'),
                error=(r0.get('error_text') or '')[:120])
            transcript['ok'] = ok
        except Exception as e:
            rec('simulate', ok=False, error=f'{type(e).__name__}: {e}')
    else:
        rec('simulate', ok=True, status=result.get('status'),
            alpha_id=result.get('alpha_id'))
        transcript['ok'] = result.get('status') in ('COMPLETE', 'WARNING')

    transcript['conclusion'] = ('✅ 표준 계정이 REST API 로 시뮬 성공 — Playwright 제거 가능'
                                if transcript['ok']
                                else '표준 계정 API 시뮬 실패 — Playwright 유지')
    _save(transcript)
    return 0 if transcript['ok'] else 1


def _save(t):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(t, f, ensure_ascii=False, indent=2)
    print(f'\n트랜스크립트 저장: {OUT}')
    print(t.get('conclusion', ''))


if __name__ == '__main__':
    sys.exit(main())
