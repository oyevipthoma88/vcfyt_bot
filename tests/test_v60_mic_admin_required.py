"""v60: '.mic on' must not die with CHAT_ADMIN_REQUIRED when the VC is off."""
import asyncio
import pytest
from helpers.vc_bridge import VCBridge


class Acct:
    def __init__(self, admin, shared):
        self.admin, self.shared, self.started = admin, shared, 0

    async def _call_input(self, chat_id):
        return object() if self.shared["vc"] else None

    async def start_voice_chat(self, chat_id):
        if not self.admin:
            return False            # Telegram: CHAT_ADMIN_REQUIRED
        self.started += 1
        self.shared["vc"] = True
        return True


def _bridge(main_admin, vc_on=False):
    shared = {"vc": vc_on}
    b = object.__new__(VCBridge)
    b.uvc, b.relay, b.source_chat = Acct(main_admin, shared), Acct(False, shared), -100
    return b


def test_main_id_starts_vc_when_spare_not_admin():
    b = _bridge(main_admin=True)
    asyncio.run(b._ensure_source_vc())
    assert b.uvc.started == 1


def test_running_vc_is_not_recreated():
    b = _bridge(main_admin=True, vc_on=True)
    asyncio.run(b._ensure_source_vc())
    assert b.uvc.started == 0


def test_clear_message_when_nobody_can_start():
    b = _bridge(main_admin=False)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(b._ensure_source_vc())
    assert "CHAT_ADMIN_REQUIRED" not in str(e.value) and "Voice Chat start" in str(e.value)
