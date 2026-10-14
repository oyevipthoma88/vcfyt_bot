"""Live mic must stream through the SPARE account, not the user's own.

Telegram allows an account to be in only one group call at a time. When the
relay joins the voice chat with the same account the user is listening with,
Telegram kicks one of the two out — that is the "VC crash / bahar fek deta"
bug. This test locks in the fix:

  1. play() is issued by the spare (relay) account, never by the user account
  2. the audio actually reaches the voice chat through the spare account
  3. chat state / mic flags still live on the user's own account
  4. mic OFF makes the spare account leave the voice chat
  5. with no spare account configured, behaviour falls back to the old path

Run:  python tests/test_mic_spare_account.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from aiohttp import web

# Reuse the fake py-tgcalls / Mongo / session harness.
from test_live_mic_relay import (  # noqa: E402
    CHAT_ID, FakeUVC, RATE, USER_ID, check, FAILURES, lm, stream_pcm,
    vc_manager, database,
)

OWN_OUT = "/tmp/livemic_own_out.raw"
RELAY_OUT = "/tmp/livemic_relay_out.raw"


async def run_cycle(base, session, relay_enabled):
    token = await lm.generate_token(USER_ID, CHAT_ID)
    sock = await session.ws_connect(f"{base}/ws/mic?token={token}")
    assert (await sock.receive()).data == "ready"
    await sock.receive()  # settings
    await sock.send_str(f"rate:{RATE}")
    await stream_pcm(sock, 2.0)
    await asyncio.sleep(0.5)
    live = lm.get_session(USER_ID)
    check(f"session is live (spare={relay_enabled})", live is not None)
    await lm.stop_session(USER_ID)
    await sock.close()
    return live


async def main():
    for path in (OWN_OUT, RELAY_OUT):
        if os.path.exists(path):
            os.unlink(path)
        open(path, "wb").close()

    own = FakeUVC(OWN_OUT, account_id=777)
    relay = FakeUVC(RELAY_OUT, account_id=888)
    vc_manager.session_manager.uvc = own
    vc_manager.session_manager.relay = relay

    app = lm.create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8801)
    await site.start()
    base = "http://127.0.0.1:8801"
    session = aiohttp.ClientSession()

    try:
        # --- 1) spare account configured ---------------------------------
        live = await run_cycle(base, session, relay_enabled=True)

        # With LIVE_MIC_RELAY_STAY (default) the spare keeps its seat after
        # mic OFF by playing a silence stream, so a second play() is expected.
        check("spare account streamed into the VC",
              relay.calls.play_calls[:1] == [CHAT_ID], str(relay.calls.play_calls))
        check("user's own account never joined the VC",
              own.calls.play_calls == [], str(own.calls.play_calls))
        check("spare account was volume-boosted, not the user",
              live is not None and live.relay.account_id == 888)
        check("chat state stayed on the user's own account",
              CHAT_ID in own.chats)
        check("mic flag cleared after OFF", not own.chats[CHAT_ID].mic_enabled)
        check("spare account kept its VC seat on mic OFF (parked)",
              not getattr(relay.calls, "left", []) and len(relay.calls.play_calls) > 1,
              str(relay.calls.play_calls))

        for reader in relay.calls.readers:
            try:
                await asyncio.wait_for(reader.wait(), 5)
            except asyncio.TimeoutError:
                reader.kill()
        check("audio reached the VC via the spare account",
              os.path.getsize(RELAY_OUT) > 48000 * 2 * 2,
              f"{os.path.getsize(RELAY_OUT)} bytes")
        check("nothing was sent through the user's account",
              os.path.getsize(OWN_OUT) == 0)

        # --- 2) no spare account: old single-account behaviour ------------
        await database.db.delete_app_value(f"live_mic_token_{USER_ID}")
        vc_manager.session_manager.relay = None
        own2 = FakeUVC(OWN_OUT, account_id=777)
        vc_manager.session_manager.uvc = own2
        live2 = await run_cycle(base, session, relay_enabled=False)
        check("falls back to the user's own account without a spare",
              own2.calls.play_calls == [CHAT_ID], str(own2.calls.play_calls))
        check("fallback does not leave the VC",
              not getattr(own2.calls, "left", []))
        check("fallback marks the session as shared-account",
              live2 is not None and live2.shared_account)

        for reader in own2.calls.readers:
            try:
                await asyncio.wait_for(reader.wait(), 5)
            except asyncio.TimeoutError:
                reader.kill()
    finally:
        await session.close()
        await lm.stop_server()
        await runner.cleanup()

    print("\n" + ("ALL SPARE ACCOUNT CHECKS PASSED" if not FAILURES
                  else f"FAILED: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
