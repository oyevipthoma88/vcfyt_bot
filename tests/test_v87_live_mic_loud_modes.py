"""v87: live mic has the same 3 modes as audio (loudest/phone/bass), is as
loud as Telegram allows, and never flat-tops / clips after Opus."""
import shutil
import subprocess

import numpy as np
import pytest

from helpers import live_mic_v79 as v79


@pytest.fixture(autouse=True)
def _reset_mode():
    old = v79._mode
    yield
    v79._mode = old


def test_three_modes_match_audio_playback_names():
    from helpers import playback_v77
    assert v79.MODES == playback_v77.MODES == ("loudest", "phone", "bass")


def test_old_mode_names_still_work():
    assert v79.set_mode("clean") == "phone"
    assert v79.set_mode("warm") == "bass"
    assert v79.set_mode("FIGHT") == "loudest"
    with pytest.raises(ValueError):
        v79.set_mode("xyz")


@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_chain_ends_dual_mono_without_3db_upmix_loss(mode):
    chain = v79.build_live_mic_filter(-0.3, mode=mode)
    parts = chain.split(",")
    assert "pan=stereo|c0=c0|c1=c0" in parts
    assert parts.index("pan=stereo|c0=c0|c1=c0") > max(
        i for i, p in enumerate(parts) if p.startswith("alimiter="))


def test_limiter_follows_telegram_ceiling():
    # 100 % listeners: just under -0.3 dBFS; admin 200 %: 6.2 dB lower.
    assert v79.limiter_db("loudest", -0.3) == pytest.approx(-1.0)
    assert v79.limiter_db("loudest", -6.5) == pytest.approx(-7.2)
    # never above the -0.3 dBFS ceiling, whatever is passed in
    assert v79.limiter_db("loudest", 3.0) == pytest.approx(-1.0)
    assert v79.limiter_db("loudest", None) == pytest.approx(-1.0)


def _voice(seconds=4, sr=48000):
    t = np.arange(sr * seconds) / sr
    f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    ph = 2 * np.pi * np.cumsum(f0) / sr
    x = sum((0.6 / k) * np.sin(k * ph) for k in range(1, 30))  # glottal-like buzz
    env = (0.15 + 0.85 * np.maximum(0, np.sin(2 * np.pi * 2.2 * t)) ** 2)
    x = x * env
    return (x / np.max(np.abs(x)) * 0.05 * 32767).astype(np.int16).tobytes()


def _through_opus(chain, pcm, bitrate):
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1",
                          "-i", "-", "-af", chain, "-ar", "48000", "-ac", "2",
                          "-c:a", "libopus", "-b:a", bitrate, "-application", "voip",
                          "-f", "ogg", "-"], input=pcm, capture_output=True, check=True).stdout
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "f32le", "-ac", "2", "-"],
                         input=out, capture_output=True, check=True).stdout
    return np.frombuffer(dec, np.float32).reshape(-1, 2)[:, 0].astype(np.float64)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
@pytest.mark.parametrize("ceiling,gain", [(-0.3, 1.0), (-6.5, 2.0)])
def test_loud_but_no_clipping_after_telegram_opus(mode, ceiling, gain):
    pcm = _voice()
    for bitrate in ("32k", "48k"):
        y = _through_opus(v79.build_live_mic_filter(ceiling, mode=mode), pcm, bitrate) * gain
        rms_db = 20 * np.log10(np.sqrt(np.mean(y[48000:] ** 2)))
        assert np.mean(np.abs(y) >= 0.999) < 0.0002, f"{mode} {bitrate}: clips after Opus"
        assert rms_db > -14.0, f"{mode} {bitrate}: only {rms_db:.1f} dBFS RMS"
