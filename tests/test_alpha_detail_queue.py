"""리더보드 상세 → 제출 대기 추가.

핵심은 소유권: 큐 추가는 클라이언트가 준 wqb_alpha_id 를 믿지 않고 alpha_pk 로
DB 를 다시 읽는다. 그 조회가 **본인 알파만** 돌려주지 않으면 남의 알파를 자기
큐에 넣을 수 있다. 읽기 전용 테스트 — 실 DB 를 변형하지 않는다.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import db


class TestGetAlphaById(unittest.TestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(db.get_alpha_by_id(999999, 1))

    def test_owner_only_and_view_shape(self):
        db.init()
        with db._DB_LOCK, db._connect() as conn:
            row = conn.execute(
                'SELECT id, user_id FROM alphas ORDER BY id DESC LIMIT 1').fetchone()
        if row is None:
            self.skipTest('alphas 비어 있음')
        pk, uid = int(row['id']), int(row['user_id'])

        a = db.get_alpha_by_id(uid, pk)
        self.assertIsNotNone(a)
        self.assertEqual(int(a['id']), pk)
        # 상세 화면이 그대로 읽는 필드 — 파싱된 형태여야 한다.
        self.assertIsInstance(a['metrics'], dict)      # metrics.wqb_alpha_id 가 제출 열쇠
        self.assertIsInstance(a['pass_items'], list)
        self.assertIsInstance(a['fail_items'], list)

        # 남의 계정으로는 보이지 않는다.
        self.assertIsNone(db.get_alpha_by_id(uid + 100000, pk))


class TestGetAlphaByCode(unittest.TestCase):
    """제출 내역 행 → 알파 상세. submit_attempts 엔 pk 가 없어 code 로 되짚는다."""

    def test_blank_and_missing_return_none(self):
        self.assertIsNone(db.get_alpha_by_code(1, ''))
        self.assertIsNone(db.get_alpha_by_code(999999, 'ts_rank(close, 5)'))

    def test_owner_only_lookup_by_code(self):
        db.init()
        with db._DB_LOCK, db._connect() as conn:
            row = conn.execute(
                'SELECT id, user_id, code FROM alphas ORDER BY id DESC LIMIT 1').fetchone()
        if row is None:
            self.skipTest('alphas 비어 있음')
        pk, uid, code = int(row['id']), int(row['user_id']), row['code']

        a = db.get_alpha_by_code(uid, code)
        self.assertIsNotNone(a)
        self.assertEqual(a['code'], code)
        self.assertIsInstance(a['metrics'], dict)
        # 같은 코드의 최신 행 — pk 조회와 같은 알파를 가리켜야 한다.
        self.assertEqual(int(a['id']), pk)
        # 남의 계정으로는 보이지 않는다.
        self.assertIsNone(db.get_alpha_by_code(uid + 100000, code))


if __name__ == '__main__':
    unittest.main()
