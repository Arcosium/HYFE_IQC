"""theme_playbook — 테마가 바뀌면 **로컬 LLM 이 스스로** 전략을 세워 첫 큐에 넣는다.

2026-07-27 사장 지시. 그전까지는 새 테마가 걸릴 때마다 사람이(또는 Claude 가)
"이 테마는 고회전이니 짧은 창 플로우 비율을 노려라" 같은 판단을 대신 내려 줬다.
이 모듈은 그 판단 과정을 **데이터로 재구성해 프롬프트에 담아** 로컬 모델에 넘긴다.

전달하는 것 (전부 라이브 실측에서 온다 — 하드코딩된 전략 훈수가 아니다):
  1. 지금 걸린 탐색 조건 (리전·유니버스·중립화 허용집합·금지 데이터셋)
  2. **활성 테마 이름과 배수** — alphas.metrics 의 themes / theme_multiplier
     (WQB MATCHES_THEMES 체크가 준 값; harvest 가 이미 저장한다)
  3. 그 테마에서 **실제로 통과한 알파의 지표 프로파일** — 어떤 회전율·샤프 대역이
     제출까지 갔는지. 없으면 '아직 없음' 이라고 알린다(추측을 만들지 않게).
  4. 테마 유형이 함의하는 설계 방향 (고회전 테마면 표준컷이 WARNING 으로 강등되는
     HT 규칙, 피라미드 배수가 붙은 데이터 계열 등)

산출: strategy_specs (= 워커가 다음 라운드에 **최우선 소비**하는 첫 큐).
실패는 전부 fail-open — 스펙이 0 이어도 GA 는 평소대로 돈다.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from . import db as _db
from . import run_config

LOG = logging.getLogger('genomicwqb.theme_playbook')

# 테마 브리핑을 만들 때 훑는 최근 알파 수 (실측 프로파일 근거).
_LOOKBACK = 400


def primary_theme(themes: dict, spec=None) -> str:
    """현재 공략해야 할 Power Pool 테마 이름을 일반 배수 테마보다 우선한다."""
    names = list((themes or {}).get('all') or [])
    pp = [name for name in names if 'power pool' in name.lower()]
    if not pp:
        return ''
    region = str(getattr(spec, 'region', '') or '').upper()
    delay = str(getattr(spec, 'delay', '') or '')
    scoped = [name for name in pp
              if (not region or f'{region}/' in name.upper())
              and (not delay or f'/D{delay}' in name.upper())]
    return (scoped or pp)[0]


def active_themes(user_id: int) -> dict:
    """최근 알파의 MATCHES_THEMES 수확에서 '지금 활성인 테마' 를 복원한다.

    → {'matched': {name: multiplier|None}, 'all': [name…], 'multiplier': '2.4'|''}
    한 번도 관측 못 했으면 빈 dict.
    """
    out = {'matched': {}, 'all': [], 'multiplier': ''}
    try:
        rows = _db.recent_metrics(user_id, limit=_LOOKBACK)
    except Exception:
        return out
    seen: list[str] = []
    for m in rows:
        for key in ('themes', 'themes_unmatched'):
            for nm in str(m.get(key) or '').split(','):
                nm = nm.strip()
                if nm and nm not in seen:
                    seen.append(nm)
        if m.get('themes') and not out['matched']:
            for nm in str(m['themes']).split(','):
                nm = nm.strip()
                if nm:
                    out['matched'][nm] = None
            out['multiplier'] = str(m.get('theme_multiplier') or '')
    out['all'] = seen
    return out


def theme_profile(user_id: int, theme_name: str) -> dict:
    """해당 테마에 **매칭된** 알파들의 실측 지표 대역 (설계 목표를 사실로 알려주려고)."""
    prof = {'n': 0, 'sharpe': [], 'turnover': [], 'submitted': 0}
    try:
        rows = _db.recent_metrics(user_id, limit=_LOOKBACK, with_submitted=True)
    except Exception:
        return prof

    def _f(v):
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return None

    for m in rows:
        if theme_name and theme_name not in str(m.get('themes') or ''):
            continue
        s, t = _f(m.get('sharpe')), _f(m.get('turnover'))
        if s is None:
            continue
        prof['n'] += 1
        prof['sharpe'].append(s)
        if t is not None:
            prof['turnover'].append(t)
        if m.get('_submitted'):
            prof['submitted'] += 1
    return prof


def _implications(names) -> list[str]:
    """테마 **이름에서 읽히는** 규칙만 적는다 (없는 규칙을 지어내지 않는다)."""
    out = []
    joined = ' '.join(names).upper()
    if 'HIGH TURNOVER' in joined:
        out.append(
            '- 활성 테마가 고회전(High Turnover)이다 → 알파가 HT 분류를 받으면 '
            'LOW_SHARPE/LOW_FITNESS 같은 표준컷이 WARNING 으로 강등된다(제출 가능). '
            'HT 분류 조건은 turnover >= 0.20 이고, Power Pool 적격 상한은 0.70 이므로 '
            '**회전율 0.2~0.7 대역**을 노려라. 짧은 창(3~10일) 시계열 변환이 그 대역을 만든다.')
    if 'PYRAMID' in joined:
        pyr = [n for n in names if 'Pyramid' in n]
        if pyr:
            out.append(f'- 피라미드 테마 배수가 붙어 있다: {", ".join(pyr)} → 그 계열'
                       ' 데이터를 쓰면 같은 성과라도 점수 배수가 커진다.')
    return out


def brief(user_id: int) -> str:
    """로컬 LLM 에 넘길 테마 브리핑 (프롬프트 블록)."""
    spec = run_config.get_constraint()
    lines = ['[현재 Power Pool 테마 / 탐색 조건]']
    lines.append(f'- 조건: {spec.describe() if spec else "(무제약)"}')
    th = active_themes(user_id)
    primary = primary_theme(th, spec)
    if primary:
        lines.append(f'- **현재 공략 대상 Power Pool 테마: {primary}**')
    if th['all']:
        lines.append(f'- 관측된 활성 테마: {", ".join(th["all"])}')
    if th['matched']:
        mult = f' (합산 배수 {th["multiplier"]})' if th['multiplier'] else ''
        lines.append(f'- 우리 알파가 **실제로 매칭에 성공한** 테마: '
                     f'{", ".join(th["matched"])}{mult}')
        for name in th['matched']:
            p = theme_profile(user_id, name)
            if p['n']:
                sh = sorted(p['sharpe'])
                to = sorted(p['turnover']) or [0.0]
                lines.append(
                    f'  · "{name}" 매칭 알파 {p["n"]}건 실측 — '
                    f'Sharpe 중앙값 {sh[len(sh)//2]:.2f} (최고 {sh[-1]:.2f}) · '
                    f'회전율 중앙값 {to[len(to)//2]:.2f} · 제출 성공 {p["submitted"]}건')
    lines += _implications(th['all'] or [])
    if not th['all']:
        lines.append('- (아직 테마 관측 데이터가 없다. 조건만 지켜 설계하라.)')
    lines.append('[Power Pool 적격 요건] 고유 데이터필드 <= 3 · 연산자 <= 8 · '
                 '회전율 1~70% · Sharpe >= 1.0 · Power Pool 알파 간 self-corr < 0.5')
    return '\n'.join(lines)


_PLAN_SYSTEM = """너는 WorldQuant Brain 의 퀀트 리서치 리드다.
새 Power Pool 테마가 걸렸다. **이번 한 번만** 깊게 생각해서 이 테마의 공략 방침을 정하라.
(이후 개별 알파 수식 설계는 다른 단계가 맡는다 — 너는 '무엇을 노릴지' 만 정한다.)

정할 것:
1. 이 테마에서 점수가 되는 조건은 무엇이고, 무엇이 병목인가.
2. 어떤 데이터 계열(family)과 어떤 신호 메커니즘을 우선할지 — 이유와 함께 3~5개.
3. 목표 회전율 대역과 그 대역을 만드는 시계열 창(lookback) 범위.
4. 피해야 할 것 (이미 실패한 방향, 상관 벽 등).

출력은 **한국어 평문 12줄 이내**. JSON·코드·수식 금지. 짧고 단정적으로."""


def make_plan(user_id: int, *, timeout: int = 900) -> str:
    """1단계 — **추론 ON, 단 1회**. 테마 공략 방침을 세운다 (2026-07-27 사장 지시).

    이 결과를 2단계(식 쓰기, 추론 OFF)의 근거로 넘긴다. 실패하면 빈 문자열이고,
    그 경우 2단계는 브리핑만으로 진행한다(fail-open).
    """
    try:
        from .ideation import _llm
        txt = _llm([{'role': 'system', 'content': _PLAN_SYSTEM},
                    {'role': 'user', 'content': brief(user_id)}],
                   temperature=0.5, timeout=timeout, think=True)
        plan = (txt or '').strip()
        if plan:
            LOG.info('테마 공략 방침 수립 (%d자)', len(plan))
        return plan[:2500]
    except Exception as e:
        LOG.warning('테마 방침 수립 실패(무시): %s', e)
        return ''


def seed_specs(user_id: int, *, k: int = 8, account_type: str = 'research_consultant') -> int:
    """테마 브리핑 → (추론 1회) 방침 → (추론 OFF) 후보 k개 → **첫 큐** 적재.

    반환: 실제로 큐에 들어간 스펙 수. 어떤 실패도 예외로 새어 나가지 않는다.
    """
    try:
        from . import strategy_spec
        spec = run_config.get_constraint()
        ev = brief(user_id)
        plan = make_plan(user_id)
        if plan:
            ev = f'{ev}\n\n[이번 테마 공략 방침 — 리서치 리드가 정함]\n{plan}'
        region = (getattr(spec, 'region', '') or 'USA')
        th = active_themes(user_id)
        headline = (primary_theme(th, spec)
                    or ', '.join(th['matched']) or ', '.join(th['all'])
                    or (spec.describe() if spec else 'no-theme'))
        hypo = {
            'title': f'{region} 테마 공략: {headline}'[:80],
            'rationale': (
                '현재 활성 Power Pool 테마의 조건과 배수, 그리고 이 계정이 그 테마에서 '
                '실측한 지표 대역을 근거로 후보를 설계한다. 조건 밖 설정(리전·유니버스·'
                '중립화·금지 데이터셋)은 시스템이 강제하므로, 신호 구조와 회전율 대역을 '
                '테마에 맞추는 것이 핵심이다.'),
            'citations': [],
        }
        # 2단계 — 식 쓰기는 **추론 OFF**. 반복 호출이라 추론을 켜면 회당 수 분씩
        # 걸리고 빈 응답·폭주 위험이 커진다 (2026-07-27 사장 지시).
        built = strategy_spec.concretize(hypo, ev, k=k, account_type=account_type,
                                         think=False)
        if not built:
            LOG.warning('테마 스펙 생성 0건 (LLM 응답/검증 실패)')
            return 0
        run_id = _db.create_research_run(
            user_id, f'[자동] 테마 플레이북 — {headline}'[:200])
        hid = _db.insert_hypothesis(run_id, user_id, hypo)
        n = 0
        for b in built:
            try:
                _db.insert_spec(hid, user_id, genome=b['genome'], code=b.get('code', ''),
                                settings=b.get('settings') or {}, delay=b.get('delay'),
                                why=b.get('why', ''))
                n += 1
            except Exception as e:
                LOG.warning('스펙 저장 실패(무시): %s', e)
        LOG.info('테마 플레이북 스펙 %d건 큐 적재 (%s)', n, headline[:60])
        return n
    except Exception as e:
        LOG.warning('테마 플레이북 실패(무시): %s', e)
        return 0


def start_background(user_id: int, *, k: int = 8,
                     account_type: str = 'research_consultant') -> None:
    """라운드를 막지 않도록 백그라운드로 돌린다 (LLM 호출이 수 분 걸린다)."""
    threading.Thread(target=seed_specs, args=(user_id,),
                     kwargs={'k': k, 'account_type': account_type},
                     daemon=True, name=f'theme-playbook-{user_id}').start()
