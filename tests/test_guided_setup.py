import unittest

from helpers.peer_guard import chat_id_variants
from helpers.setup_flow import mic_setup_step


class GuidedSetupTests(unittest.TestCase):
    def test_steps_follow_prerequisites(self):
        self.assertEqual(mic_setup_step(False, True, 1, 2), "login")
        self.assertEqual(mic_setup_step(True, False, 1, 2), "spare")
        self.assertEqual(mic_setup_step(True, True, None, 2), "src")
        self.assertEqual(mic_setup_step(True, True, 1, None), "tgt")
        self.assertEqual(mic_setup_step(True, True, 1, 2), "ready")

    def test_chat_id_without_minus_100_is_accepted(self):
        self.assertEqual(chat_id_variants("1234567890")[0], -1001234567890)
        self.assertEqual(chat_id_variants("-1234567890")[0], -1001234567890)

    def test_full_chat_id_kept(self):
        self.assertEqual(chat_id_variants("-1001234567890"), [-1001234567890])

    def test_zero_rejected(self):
        self.assertEqual(chat_id_variants("0"), [])

    def test_premium_emoji_in_payload(self):
        from pyrogram.types import InlineKeyboardMarkup
        from plugins.ui import B
        from helpers.bot_api_styles import markup_payload
        p = markup_payload(InlineKeyboardMarkup([[B("🎤 Live Mic", callback_data="x")]]))
        self.assertEqual(p["inline_keyboard"][0][0]["icon_custom_emoji_id"], "5429413328768224565")

    def test_logged_out_home_is_simple(self):
        from plugins.ui import home_kb
        self.assertEqual(len(home_kb(False, False).inline_keyboard), 2)


if __name__ == "__main__":
    unittest.main()
