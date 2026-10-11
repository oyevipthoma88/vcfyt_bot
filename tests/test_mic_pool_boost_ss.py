"""Spare pool lease, 200% admin boost detection, SS prerender."""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from helpers import mic_tools  # noqa: E402
from helpers.vc_manager import SessionManager  # noqa: E402

A, B = "A" * 40, "B" * 40


class PoolTests(unittest.TestCase):
    def setUp(self):
        self._old = (Config.ASSISTANT_SESSIONS, Config.ASSISTANT_SESSION)
        Config.ASSISTANT_SESSIONS, Config.ASSISTANT_SESSION = f"{A}, {B}", ""
        import helpers.vc_bridge as vb
        self.vb = vb
        vb._bridges.clear()

    def tearDown(self):
        Config.ASSISTANT_SESSIONS, Config.ASSISTANT_SESSION = self._old
        self.vb._bridges.clear()

    def test_pool_parses_comma_separated_sessions(self):
        self.assertEqual(SessionManager.assistant_pool(), [A, B])

    def test_lease_is_sticky_for_same_user(self):
        sm = SessionManager()
        first = sm.lease_pool(7)
        self.assertEqual(sm.lease_pool(7), first)

    def test_busy_spare_is_not_given_to_second_user(self):
        sm = SessionManager()
        s1 = sm.lease_pool(1)
        self.vb._bridges[1] = object()          # user 1 is live on s1
        s2 = sm.lease_pool(2)
        self.assertNotEqual(s1, s2)

    def test_no_pool_means_no_spare(self):
        Config.ASSISTANT_SESSIONS = ""
        self.assertEqual(SessionManager().lease_pool(5), "")


class AdminBoostTests(unittest.TestCase):
    def _member(self, status, video):
        return types.SimpleNamespace(status=status, privileges=types.SimpleNamespace(
            can_manage_video_chats=video))

    def test_admin_with_video_right_counts(self):
        self.assertTrue(mic_tools._is_vc_admin(self._member("ChatMemberStatus.ADMINISTRATOR", True)))

    def test_admin_without_video_right_does_not_count(self):
        self.assertFalse(mic_tools._is_vc_admin(self._member("ChatMemberStatus.ADMINISTRATOR", False)))

    def test_plain_member_does_not_count(self):
        self.assertFalse(mic_tools._is_vc_admin(self._member("ChatMemberStatus.MEMBER", False)))

    def test_auto_admin_can_be_disabled(self):
        os.environ["LIVE_MIC_AUTO_ADMIN"] = "0"
        try:
            relay = types.SimpleNamespace(account_id=9, client=types.SimpleNamespace(
                get_chat_member=lambda *a: (_ for _ in ()).throw(RuntimeError())))
            ok = asyncio.run(mic_tools.ensure_relay_admin(types.SimpleNamespace(client=None), relay, -100))
            self.assertFalse(ok)
        finally:
            os.environ.pop("LIVE_MIC_AUTO_ADMIN", None)

    def test_room_link_strips_minus_100(self):
        self.assertEqual(mic_tools.room_link(-1001234567890), "https://t.me/c/1234567890")


class ScreenShareTests(unittest.TestCase):
    def test_prerender_matches_screen_size(self):
        from PIL import Image
        from helpers.audio_processor import prerender_screen_image, _SS_DEFAULT
        out = prerender_screen_image(_SS_DEFAULT, 1280, 720)
        self.assertEqual(Image.open(out).size, (1280, 720))

    def test_screen_command_skips_per_frame_scaling(self):
        from helpers.audio_processor import build_fake_screen_command
        cmd = build_fake_screen_command(1280, 720, 15)
        self.assertNotIn("scale=1280", cmd)
        self.assertIn("full_chroma_int", cmd)


if __name__ == "__main__":
    unittest.main()
