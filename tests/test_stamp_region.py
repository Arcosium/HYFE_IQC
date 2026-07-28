# tests/test_stamp_region.py
# 2026-07-28 실측 버그: 레이어 주입 후보(재조합·사냥사다리·HT구제·개선)가 region 을
# 안 실어 wqb_api._full_settings 기본값 'USA' 로 떨어졌고, GLB 유니버스와 충돌해
# 400 으로 조용히 죽었다 — 한 라운드 4건.
from server.worker import _stamp_region


def test_layer_candidate_without_region_gets_the_round_region():
    """부모 알파 설정만 물려받은 후보(= region 없음)에 조건 리전이 채워진다."""
    cands = [{'idx': 31, 'settings': {'universe': 'TOPDIV3000',
                                      'neutralization': 'CROWDING'}}]
    assert _stamp_region(cands, 'GLB') == 1
    assert cands[0]['settings']['region'] == 'GLB'
    assert cands[0]['settings']['universe'] == 'TOPDIV3000', '나머지 설정은 건드리면 안 된다'


def test_existing_region_is_never_overwritten():
    """GA 후보는 이미 리전을 실어 온다 — 덮어쓰면 탐색 조건을 왜곡한다."""
    cands = [{'idx': 1, 'settings': {'region': 'USA', 'universe': 'TOP3000'}}]
    assert _stamp_region(cands, 'GLB') == 0
    assert cands[0]['settings']['region'] == 'USA'


def test_settings_key_missing_entirely_is_created():
    cands = [{'idx': 51}]
    assert _stamp_region(cands, 'GLB') == 1
    assert cands[0]['settings'] == {'region': 'GLB'}


def test_no_constraint_region_leaves_everything_alone():
    """조건 리전이 없으면 예전과 똑같이 둔다 — 여기서 임의로 정하지 않는다."""
    cands = [{'idx': 31, 'settings': {'universe': 'TOPDIV3000'}}]
    for empty in (None, ''):
        assert _stamp_region(cands, empty) == 0
    assert 'region' not in cands[0]['settings']


def test_empty_region_string_counts_as_missing():
    """region='' 은 _full_settings 에서 falsy 라 'USA' 로 떨어진다 — 미지정과 같다."""
    cands = [{'idx': 32, 'settings': {'region': '', 'universe': 'TOPDIV3000'}}]
    assert _stamp_region(cands, 'GLB') == 1
    assert cands[0]['settings']['region'] == 'GLB'


def test_stamped_region_reaches_the_submitted_payload():
    """지문/제출까지 실제로 흘러가는지 — _full_settings 가 USA 로 덮지 않아야 한다."""
    from server.wqb_api import WqbApiClient
    cands = [{'idx': 31, 'settings': {'universe': 'TOPDIV3000'}}]
    _stamp_region(cands, 'GLB')
    full = WqbApiClient._full_settings(cands[0]['settings'])
    assert full['region'] == 'GLB' and full['universe'] == 'TOPDIV3000'
