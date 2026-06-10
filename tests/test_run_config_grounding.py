from __future__ import annotations
import sys, os, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest


class TestGroundingFlag(unittest.TestCase):
    def setUp(self):
        from server import run_config
        self.rc = run_config
        self._orig = self.rc.is_grounding_enabled()

    def tearDown(self):
        self.rc.set_grounding_enabled(self._orig)

    def test_default_is_true_when_key_absent(self):
        from unittest import mock
        with mock.patch.object(self.rc, '_read', return_value={}):
            self.assertTrue(self.rc.is_grounding_enabled())

    def test_round_trip_false(self):
        self.rc.set_grounding_enabled(False)
        self.assertFalse(self.rc.is_grounding_enabled())
        self.rc.set_grounding_enabled(True)
        self.assertTrue(self.rc.is_grounding_enabled())


if __name__ == '__main__':
    unittest.main()
