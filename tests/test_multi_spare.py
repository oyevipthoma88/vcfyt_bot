"""Tests for Multi-Spare ID and Screen-Share exit behavior."""

import os
import sys
import unittest
from unittest.mock import MagicMock
from importlib.machinery import ModuleSpec

# Auto mock external dependencies not needed for unit logic
class _MockFinder:
    def find_spec(self, fullname, path, target=None):
        if any(fullname.startswith(p) for p in ["pyrogram", "pytgcalls", "ntgcalls"]):
            return ModuleSpec(fullname, _MockLoader(), is_package=True)
        return None

class _MockLoader:
    def create_module(self, spec):
        return MagicMock()
    def exec_module(self, module):
        pass

sys.meta_path.insert(0, _MockFinder())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import access_control
from config import Config


class MultiSpareTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._db_store = {}
        self._premium_users = set()

        class MockDB:
            def __init__(self, outer):
                self._o = outer
            async def get_app_value(self, key):
                return self._o._db_store.get(key)
            async def set_app_value(self, key, val):
                self._o._db_store[key] = str(val)
            async def delete_app_value(self, key):
                self._o._db_store.pop(key, None)
            async def get_premium(self, uid):
                return {"user_id": uid, "premium_until": "2099-01-01T00:00:00Z"} if uid in self._o._premium_users else None
            async def get_linked_user_ids(self, uid):
                return [uid]

        self.mock_db = MockDB(self)
        access_control.db = self.mock_db

    async def test_access_control_free_and_premium(self):
        # 1. Default should be Free for all users
        self.assertFalse(await access_control.multi_spare_premium_only())
        self.assertTrue(await access_control.can_multi_spare(12345))

        # 2. Toggle to Premium-only
        await access_control.set_multi_spare_premium_only(True)
        self.assertTrue(await access_control.multi_spare_premium_only())

        # Regular user blocked
        self.assertFalse(await access_control.can_multi_spare(12345))

        # Owner allowed
        Config.OWNER_IDS = [99999]
        self.assertTrue(await access_control.can_multi_spare(99999))

        # Premium user allowed
        self._premium_users.add(12345)
        self.assertTrue(await access_control.can_multi_spare(12345))

        # 3. Toggle back to Free
        await access_control.set_multi_spare_premium_only(False)
        self.assertTrue(await access_control.can_multi_spare(54321))

    async def test_spare_slots_enumeration(self):
        import helpers.vc_manager as vcm
        vcm._db = lambda: self.mock_db
        sm = vcm.session_manager

        uid = 1001
        self.assertEqual(sm.spare_key(uid, 1), f"assistant_session_{uid}")
        self.assertEqual(sm.spare_key(uid, 2), f"assistant_session_{uid}_2")
        self.assertEqual(sm.spare_key(uid, 5), f"assistant_session_{uid}_5")

        self.assertEqual(await sm.spare_slots(uid), [])

        self._db_store[sm.spare_key(uid, 1)] = "tok_main"
        self._db_store[sm.spare_key(uid, 2)] = "tok_extra2"
        self._db_store[sm.spare_key(uid, 4)] = "tok_extra4"

        slots = await sm.spare_slots(uid)
        self.assertEqual(slots, [1, 2, 4])
        self.assertEqual(await sm.assistant_string(uid, 1), "tok_main")
        self.assertEqual(await sm.assistant_string(uid, 2), "tok_extra2")
        self.assertEqual(await sm.assistant_string(uid, 3), "")


if __name__ == "__main__":
    unittest.main()
