from server import settings_fp as sf


def test_default_equivalence():
    # 부분 dict 와 기본값-동치 dict 는 같은 fingerprint.
    a = sf.settings_fingerprint(sf.effective_settings({'universe': 'TOP3000'}, '1'))
    b = sf.settings_fingerprint(sf.effective_settings({}, '1'))
    assert a == b


def test_numeric_normalization():
    a = sf.settings_fingerprint(sf.effective_settings({'truncation': '0.010', 'decay': '4.0'}, '1'))
    b = sf.settings_fingerprint(sf.effective_settings({'truncation': '0.01', 'decay': '4'}, '1'))
    assert a == b


def test_case_normalization():
    a = sf.settings_fingerprint(sf.effective_settings({'universe': 'top3000'}, '1'))
    b = sf.settings_fingerprint(sf.effective_settings({'universe': 'TOP3000'}, '1'))
    assert a == b


def test_delay_injected_and_partial_delay_ignored():
    d0 = sf.settings_fingerprint(sf.effective_settings({'delay': '1'}, '0'))
    d1 = sf.settings_fingerprint(sf.effective_settings({'delay': '0'}, '1'))
    assert d0 != d1
    assert sf.effective_settings({'delay': '1'}, '0')['delay'] == '0'


def test_different_settings_differ():
    a = sf.settings_fingerprint(sf.effective_settings({'neutralization': 'SECTOR'}, '1'))
    b = sf.settings_fingerprint(sf.effective_settings({'neutralization': 'INDUSTRY'}, '1'))
    assert a != b


def test_deterministic():
    eff = sf.effective_settings({'universe': 'TOP500'}, '1')
    assert sf.settings_fingerprint(eff) == sf.settings_fingerprint(eff)
