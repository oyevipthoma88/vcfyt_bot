"""v50: clean-loud master (no square wave) + saved logins are never lost."""
import asyncio
import os
import re
import subprocess
import tempfile

import numpy as np
import pytest

from helpers.audio_processor import build_ffmpeg_filter, build_live_mic_filter, _sanitize_ffmpeg_filter

VOICE = ("aevalsrc='(0.5*sin(2*PI*150*t)+0.3*sin(2*PI*300*t)+0.15*sin(2*PI*900*t)"
         "+0.1*sin(2*PI*2500*t))*(0.2+0.8*abs(sin(2*PI*2.5*t)))':s=48000:d=6")


def _measure(chain: str):
    with tempfile.TemporaryDirectory() as d:
        wav, opus = os.path.join(d, "o.wav"), os.path.join(d, "o.opus")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", VOICE, "-af",
                        "volume=-18dB," + chain, "-ar", "48000", "-ac", "1", wav], check=True)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", wav, "-af", "ebur128=peak=true",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
        lufs = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])
        tp = float(re.findall(r"Peak:\s+(-?[\d.]+)", r)[-1])
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "48k",
                        "-application", "voip", opus], check=True)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", opus, "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
        x = np.frombuffer(raw, dtype=np.float32)
        return lufs, tp, float(np.mean(np.abs(x) >= 0.999))


@pytest.mark.parametrize("name", ["playback", "live"])
def test_loud_clean_and_survives_opus(name):
    chain = (_sanitize_ffmpeg_filter(build_ffmpeg_filter(volume=1000)) if name == "playback"
             else build_live_mic_filter())
    lufs, tp, clip = _measure(chain)
    assert lufs > -13.0, f"{name}: {lufs} LUFS too quiet"
    assert lufs < -7.0, f"{name}: {lufs} LUFS = square wave (Telegram AGC ducks it)"
    assert tp <= -0.8, f"{name}: true peak {tp}"
    assert clip < 0.005, f"{name}: {clip:.2%} samples clip after Opus"


def test_playback_and_live_land_at_same_loudness():
    a, _, _ = _measure(_sanitize_ffmpeg_filter(build_ffmpeg_filter(volume=1000)))
    b, _, _ = _measure(build_live_mic_filter())
    assert abs(a - b) < 3.0


def test_only_real_dead_key_errors_clear_a_session():
    from helpers.vc_manager import _is_invalid_session

    class AuthKeyUnregistered(Exception):
        pass

    assert _is_invalid_session(AuthKeyUnregistered("dead"))
    assert _is_invalid_session(Exception("[401 AUTH_KEY_UNREGISTERED] key gone"))
    # these used to wipe valid sessions
    assert not _is_invalid_session(Exception("Peer -1001401234567 not found"))
    assert not _is_invalid_session(OSError("Connection lost after 401 ms"))
    assert not _is_invalid_session(Exception("FLOOD_WAIT_X: wait 1401 seconds"))


def test_session_saved_in_sqlite_wins_over_stale_mongo(tmp_path):
    from helpers.database import Database

    db = Database.__new__(Database)
    db._sqlite_path = str(tmp_path / "t.db")
    db._quota_blocked_until = 0.0
    db._last_purge = 0.0
    db._purging = False
    db._init_sqlite()

    class Users:
        async def find_one(self, q):
            return {"user_id": q["user_id"], "string_session": ""}  # stale / empty

        async def update_one(self, *a, **k):
            raise Exception("you are over your space quota, writes are blocked")

    class Mongo:
        users = Users()

    db._use_mongo, db._mongo = True, Mongo()
    asyncio.run(db.update_string(42, "NEW_SESSION"))
    assert asyncio.run(db.get_user(42))["string_session"] == "NEW_SESSION"
