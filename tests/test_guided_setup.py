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

    def test_mic_panel_is_simple(self):
        from plugins.vc_bridge import loud_kb
        for live in (False, True):
            rows = loud_kb(live).inline_keyboard
            self.assertLessEqual(len(rows), 4)
            self.assertEqual(len(rows[0]), 1)  # sirf ek bada ON/OFF button
        self.assertEqual(loud_kb(False).inline_keyboard[0][0].callback_data, "brc:on")
        self.assertEqual(loud_kb(True).inline_keyboard[0][0].callback_data, "brc:off")

    def test_every_panel_button_has_handler(self):
        import re
        from plugins.vc_bridge import loud_kb
        ok = re.compile(r"^(brc:(on|off|status|leave|loud|help)|brl:.+|brv:(tune|setup|more)|"
                        r"brs:(src|tgt|auto)|tut:video|menu:home|mic:(acct|chat))$")
        for page in ("main", "more", "tune", "setup"):
            for row in loud_kb(False, page).inline_keyboard:
                for b in row:
                    self.assertRegex(b.callback_data, ok)


if __name__ == "__main__":
    unittest.main()
