"""Tests for server/datafield_palette.py

Covers:
  - Basic structure and line count
  - Region filter (all fields are USA)
  - delay filter with relax (delay='0' rows are sparse → relax kicks in)
  - Rarity bucket contribution (gem bucket injects low-popularity fields)
  - Rotation: seed=0 vs seed=1000 differ
  - Determinism: same seed → same output
  - Robustness: missing CSV → returns '' without raising
  - Late-CSV coverage: a field from original row >200 CAN appear for some seed
"""
from __future__ import annotations

import csv
import os

import pytest

# The module under test
from server import datafield_palette as dp

# Absolute path to the real CSV (used to spot-check field metadata)
_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', 'server', 'IQC_brain_datafields.csv')
_CSV_PATH = os.path.normpath(_CSV_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_csv_rows() -> list[dict]:
    """팔레트가 실제로 읽는 소스와 **같은 것**을 본다 = 라이브 CSV ∪ 정적 CSV.

    ⚠ 예전엔 정적 CSV 만 읽었는데, 그건 라이브 CSV 가 없던 시절에만 맞는 가정이었다.
      라이브 수집(scripts/refresh_datafields.py)이 한 번 돌면 팔레트는 두 파일의
      합집합을 쓰므로, 정적 CSV 만 기대하는 테스트는 환경에 따라 깨진다
      (2026-07-21 D0 팔레트 도입 때 실제로 깨졌다).
    """
    return dp._all_rows()


def _palette_field_names(text: str) -> list[str]:
    """Extract field names from palette text (skip header line starting with '#')."""
    names = []
    for line in text.splitlines():
        if line.startswith('#'):
            continue
        parts = line.split('|')
        if parts:
            name = parts[0].strip()
            if name:
                names.append(name)
    return names


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicStructure:
    def test_returns_nonempty_string(self):
        result = dp.build_palette(n=55)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_line_count_bounded(self):
        result = dp.build_palette(n=55)
        lines = [l for l in result.splitlines() if l.strip()]
        # header (1) + up to 55 fields = 56 max
        assert len(lines) <= 56, f"Expected ≤56 lines, got {len(lines)}"

    def test_header_present(self):
        result = dp.build_palette(n=55)
        assert result.startswith('#'), "First line should be a # header"

    def test_returns_at_least_n_fields(self):
        result = dp.build_palette(n=55)
        names = _palette_field_names(result)
        assert len(names) >= 50, f"Expected ~55 fields, got {len(names)}"

    def test_field_line_format(self):
        result = dp.build_palette(n=55)
        for line in result.splitlines():
            if line.startswith('#') or not line.strip():
                continue
            assert '|' in line, f"Expected pipe-separated format: {line!r}"
            assert 'cov=' in line
            assert 'pop=' in line


class TestRegionFilter:
    def test_all_fields_are_usa(self):
        """Every returned field must exist in the CSV with region=USA."""
        result = dp.build_palette(region='USA', n=55, seed=0)
        names = _palette_field_names(result)
        assert names, "Palette should have fields"

        # Build lookup from CSV
        csv_region = {row['name']: row['region'] for row in _all_csv_rows()}
        for name in names[:10]:  # spot-check first 10
            assert name in csv_region, f"Field {name!r} not in CSV"
            assert csv_region[name] == 'USA', \
                f"Field {name!r} has region {csv_region[name]!r}, expected USA"

    def test_case_insensitive_region(self):
        """Region match is case-insensitive."""
        result_upper = dp.build_palette(region='USA', n=10)
        result_lower = dp.build_palette(region='usa', n=10)
        # Both should return non-empty results
        assert len(_palette_field_names(result_upper)) > 0
        assert len(_palette_field_names(result_lower)) > 0


class TestDelayFilter:
    def test_delay_0_returns_sufficient_fields(self):
        """delay='0' has 0 matching rows in the CSV (all rows are delay=1).
        Relax should kick in and return ~55 fields from the full pool."""
        result = dp.build_palette(delay='0', n=55, seed=0)
        names = _palette_field_names(result)
        # Should relax and still return a full palette
        assert len(names) >= 40, \
            f"delay='0' relax should return ≥40 fields, got {len(names)}"

    def test_delay_1_returns_full_palette(self):
        """delay='1' matches all rows — palette should be full."""
        result = dp.build_palette(delay='1', n=55, seed=0)
        names = _palette_field_names(result)
        assert len(names) >= 50

    def test_no_crash_on_weird_delay(self):
        """Non-existent delay value triggers relax gracefully."""
        result = dp.build_palette(delay='999', n=55, seed=0)
        assert isinstance(result, str)
        # After relax, should still return fields
        names = _palette_field_names(result)
        assert len(names) >= 40

    def test_relaxed_delay_header_shows_relaxed_annotation(self):
        """When delay filter is relaxed (no matching rows), the header must say
        delay=<val>(relaxed) so callers know the filter was not honoured."""
        result = dp.build_palette(delay='999', n=55, seed=0)
        # Find the header line
        header = next((l for l in result.splitlines() if l.startswith('#')), '')
        assert 'relaxed' in header, (
            f"Expected 'relaxed' in header when delay='999' is relaxed, got: {header!r}"
        )


class TestRarityBucket:
    def test_gem_bucket_contributes_low_popularity_fields(self):
        """Among returned fields, some should have alphas below the median.
        This proves the undiscovered-gems bucket is contributing."""
        import statistics as _stats
        result = dp.build_palette(n=55, seed=0)
        names = set(_palette_field_names(result))
        assert names

        # Look up alphas values for returned fields
        csv_alphas = {row['name']: int(row['alphas']) for row in _all_csv_rows()}
        returned_alphas = [csv_alphas[n] for n in names if n in csv_alphas]
        assert returned_alphas

        overall_median = _stats.median(csv_alphas.values())
        # At least some returned fields should be below median
        below_median = sum(1 for a in returned_alphas if a < overall_median)
        assert below_median > 0, \
            f"Expected some fields below median alphas ({overall_median}), got 0"

    def test_not_all_fields_are_high_popularity(self):
        """Palette should not be uniformly high-alphas (i.e., gems bucket works)."""
        import statistics as _stats
        result = dp.build_palette(n=55, seed=0)
        names = _palette_field_names(result)
        csv_alphas = {row['name']: int(row['alphas']) for row in _all_csv_rows()}
        returned_alphas = [csv_alphas[n] for n in names if n in csv_alphas]
        if not returned_alphas:
            pytest.skip("No matching alphas found")
        # If ALL fields had > 1000 alphas, gems bucket is broken
        very_high = sum(1 for a in returned_alphas if a > 1000)
        assert very_high < len(returned_alphas), \
            "All returned fields have very high popularity — gems bucket not working"


class TestRotation:
    def test_seed_0_differs_from_seed_1000(self):
        """Different seeds should produce different palettes (rotation works)."""
        p0 = dp.build_palette(seed=0, n=55)
        p1000 = dp.build_palette(seed=1000, n=55)
        assert p0 != p1000, \
            "seed=0 and seed=1000 produced identical palettes — rotation not working"

    def test_large_seed_does_not_crash(self):
        """Very large seed values should work (modulo wraps correctly)."""
        result = dp.build_palette(seed=999_999_999, n=55)
        assert len(_palette_field_names(result)) > 0

    def test_negative_seed_handled(self):
        """Negative seed: Python modulo with negative numbers still works; no crash."""
        # Python % is always non-negative when divisor > 0, but be safe
        result = dp.build_palette(seed=-1, n=55)
        assert isinstance(result, str)


class TestDeterminism:
    def test_same_seed_same_output(self):
        """Calling build_palette with the same seed twice returns identical output."""
        # Force cache reset between calls to ensure the CSV mtime path is exercised
        p1 = dp.build_palette(seed=7, n=55)
        p2 = dp.build_palette(seed=7, n=55)
        assert p1 == p2, "Same seed must produce identical palette (determinism)"

    def test_same_seed_with_delay(self):
        p1 = dp.build_palette(seed=42, delay='1', n=30)
        p2 = dp.build_palette(seed=42, delay='1', n=30)
        assert p1 == p2


class TestRobustness:
    def test_missing_csv_returns_empty_string(self, monkeypatch, tmp_path):
        """When the CSV path doesn't exist, build_palette returns '' without raising."""
        nonexistent = str(tmp_path / 'does_not_exist.csv')
        # Clear module cache so missing path is actually tried
        monkeypatch.setattr(dp, '_CSV_CACHE', {})
        result = dp.build_palette(_csv_path=nonexistent, n=55)
        assert result == '', f"Expected '' for missing CSV, got {result!r}"

    def test_corrupt_csv_returns_empty_string(self, monkeypatch, tmp_path):
        """A CSV with only a header and no data rows returns '' (nothing to select)."""
        bad_csv = tmp_path / 'empty.csv'
        bad_csv.write_text('name,category,coverage,description,type,'
                            'date_coverage_pct,alphas,region,universe,delay\n')
        monkeypatch.setattr(dp, '_CSV_CACHE', {})
        result = dp.build_palette(_csv_path=str(bad_csv), n=55)
        assert result == ''

    def test_never_raises_on_bad_input(self, monkeypatch, tmp_path):
        """Truly garbage file content — should return '' not raise."""
        bad = tmp_path / 'garbage.csv'
        bad.write_bytes(b'\xff\xfe not valid utf-8 at all!!!')
        monkeypatch.setattr(dp, '_CSV_CACHE', {})
        result = dp.build_palette(_csv_path=str(bad), n=55)
        assert isinstance(result, str)  # either '' or something, but no exception


class TestLateCSVCoverage:
    """Proves the palette draws from the WHOLE CSV, not just the alphabetical head.

    The rotation window is offset by `seed % len(pool)`, so using
    `seed = sorted_position_of_target` puts the target directly in the
    rotation window — a deterministic, fast proof that coverage is global.
    """

    def _sorted_index(self, name: str) -> int:
        """Return the index of `name` in the alphabetically-sorted field list.

        ⚠ **이름 기준으로 중복 제거**해야 한다. 합집합 소스에는 같은 필드가 D0·D1
          두 행으로 들어 있어서, 중복을 안 지우면 여기서 센 위치와 build_palette 의
          회전 오프셋이 어긋난다(2026-07-21 D0 팔레트 도입 때 실제로 어긋났다).
        """
        sorted_names = sorted({row['name'] for row in _all_csv_rows()})
        try:
            return sorted_names.index(name)
        except ValueError:
            return 0

    def test_nws12_field_appears_at_correct_seed(self):
        """nws12_* fields are alphabetically near the VERY END of the sorted CSV
        (original row 5088+, sorted index ~5090).  The rotation window at
        seed = sorted_index should include the target field.
        """
        rows = _all_csv_rows()
        nws_fields = [r['name'] for r in rows if r['name'].startswith('nws12_')]
        assert nws_fields, "Expected nws12_* fields in the CSV"
        target = nws_fields[0]

        # Compute the expected seed: the sorted position of this field
        seed = self._sorted_index(target)
        assert seed > 200, \
            f"Target {target!r} should be alphabetically late (sorted idx > 200), got {seed}"

        result = dp.build_palette(seed=seed, n=55)
        assert target in result, (
            f"Field {target!r} (sorted idx {seed}) not in palette at seed={seed}. "
            "Rotation window is not covering the full CSV."
        )

    def test_mdl177_field_appears_at_correct_seed(self):
        """mdl177_* fields are at sorted index ~1925, far beyond the first 80 rows.
        Using seed = sorted_index surfaces them deterministically.
        """
        rows = _all_csv_rows()
        mdl_fields = sorted(r['name'] for r in rows if r['name'].startswith('mdl177_'))
        assert mdl_fields, "Expected mdl177_* fields in the CSV"
        target = mdl_fields[0]

        seed = self._sorted_index(target)
        assert seed > 200, \
            f"Target {target!r} should be alphabetically late (sorted idx > 200), got {seed}"

        result = dp.build_palette(seed=seed, n=55)
        assert target in result, (
            f"Field {target!r} (sorted idx {seed}) not in palette at seed={seed}."
        )

    def test_late_field_absent_at_seed_0(self):
        """A field at sorted index ~5090 should NOT appear at seed=0 (proving
        the window actually rotates — it's not just dumping everything)."""
        rows = _all_csv_rows()
        nws_fields = [r['name'] for r in rows if r['name'].startswith('nws12_')]
        if not nws_fields:
            pytest.skip("No nws12_* fields in CSV")
        target = nws_fields[0]

        result_seed0 = dp.build_palette(seed=0, n=55)
        # Target should NOT be in seed=0 (it's at index 5090, far from offset 0)
        # (seed=0 rotation starts from index 0, covers ~17 rotation slots → indices 0..16)
        assert target not in result_seed0, (
            f"Field {target!r} at sorted idx 5090 should not appear at seed=0 "
            "— the rotation window would be too far away."
        )
