"""v97 TG-LIMIT: output stays under what Telegram's 32k Opus + the
listener's 200 % (hard x2 clip) can carry, so phones get loud audio
without "fatna" / square wave."""
import asyncio
import re
import shutil
import subprocess
import types

import numpy as np
import pytest

import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers import playback_v77 as v77
from helpers import playback_v97 as v97

HAS_FF = shutil.which("ffmpeg") is not None
MAX = dict(volume=3000, boost=10, gain=500, treble=100, bass=14)


@pytest.fixture(autouse=True)
def _restore_mode(monkeypatch):
    monkeypatch.delenv("PLAY_TG_TRIM_DB", raising=False)
    old = v77.get_mode()
    yield
    v77.set_mode(old)


def _trim(chain):
    m = re.search(r",volume=-(\d+\.\d+)dB$", chain)
    return float(m.group(1)) if m else None


@pytest.mark.parametrize("mode,db", [("loudest", 0.0), ("phone", 0.0), ("bass", 0.0)])
def test_each_mode_ends_with_its_codec_trim(mode, db):
    v77.set_mode(mode)
    assert _trim(ap.build_ffmpeg_filter(**MAX)) == pytest.approx(db, abs=0.01)


@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_confirmed_200_percent_takes_6_db_more(mode):
    v77.set_mode(mode)
    base = _trim(ap.build_ffmpeg_filter(**MAX))
    with v97.listener_boost(True):
        boosted = _trim(ap.build_ffmpeg_filter(**MAX))
    assert boosted - base == pytest.approx(6.02, abs=0.01)
    assert _trim(ap.build_ffmpeg_filter(**MAX)) == base  # context restored


def test_env_zero_turns_codec_trim_off(monkeypatch):
    monkeypatch.setenv("PLAY_TG_TRIM_DB", "0")
    v77.set_mode("loudest")
    assert _trim(ap.build_ffmpeg_filter(**MAX)) == 0.0


def test_live_mic_not_trimmed():
    assert not ap.build_ffmpeg_filter(live=True).endswith("dB") or \
        _trim(ap.build_ffmpeg_filter(live=True)) is None


def test_mute_stays_last(monkeypatch):
    monkeypatch.setenv("PLAY_ALLOW_QUIET", "1")
    assert ap.build_ffmpeg_filter(volume=0).endswith("volume=0")


def test_loudest_and_phone_get_6_band_squash_bass_does_not():
    for mode, lp in (("loudest", 4300), ("phone", 6500)):
        v77.set_mode(mode)
        c = ap.build_ffmpeg_filter(**MAX)
        assert c.count("mcompand=") == 1
        assert c.index("mcompand=") < c.index(f"lowpass=f={lp}:p=2")
    v77.set_mode("bass")
    assert "mcompand=args='0.002" not in ap.build_ffmpeg_filter(**MAX)


def _fake_mgr(confirmed, path="/a.mp3"):
    st = types.SimpleNamespace(is_playing=True, mic_enabled=False, current_file=path,
                               boost_confirmed=confirmed)
    calls = []

    async def reapply(chat_id):
        calls.append(chat_id)
        return True
    return types.SimpleNamespace(chats={1: st}, reapply=reapply), calls


def test_song_releveled_once_when_200_percent_answer_differs():
    from helpers.vc_manager import UserVC as VCManager
    mgr, calls = _fake_mgr(confirmed=True)
    assert asyncio.run(VCManager._reconcile_master(mgr, 1, "/a.mp3", False)) is True
    assert asyncio.run(VCManager._reconcile_master(mgr, 1, "/a.mp3", False)) is False
    assert calls == [1]


def test_no_relevel_when_level_already_right_or_song_changed():
    from helpers.vc_manager import UserVC as VCManager
    mgr, calls = _fake_mgr(confirmed=True)
    assert asyncio.run(VCManager._reconcile_master(mgr, 1, "/a.mp3", True)) is False
    assert asyncio.run(VCManager._reconcile_master(mgr, 1, "/b.mp3", False)) is False
    assert calls == []


def _listener_clip(path, gain):
    """Telegram path: Opus mono 32k / 60 ms -> decode -> listener x gain."""
    ogg = path + ".ogg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-ac", "1", "-c:a", "libopus",
                    "-b:a", "32k", "-application", "voip", "-frame_duration", "60", ogg], check=True)
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", ogg, "-f", "f32le", "-ac", "1", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32) * gain
    return 100.0 * np.mean(np.abs(x) > 1.0)


@pytest.mark.skipif(not HAS_FF, reason="ffmpeg missing")
def test_200_percent_listener_not_squared(tmp_path, monkeypatch):
    """Old chain: ~half the samples chopped flat at 200 %.  v97: a few %."""
    # Safety opt-in remains available; maximum defaults permit distortion.
    monkeypatch.setenv("PLAY_TG_TRIM_DB", "3.0")
    v77.set_mode("loudest")
    src = str(tmp_path / "s.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=6:c=pink:a=0.4:seed=7", "-ar", "48000", "-ac", "2", src], check=True)
    with v97.listener_boost(True):
        out = asyncio.run(ap.process_audio_to_file(src, str(tmp_path / "o.wav"), **MAX))
    assert _listener_clip(out, 2.0) < 5.0
