"""Measured playback rules, including actual FFmpeg/Opus processing.

No receiver-side Telegram AGC behavior is assumed or tested here.
"""
import asyncio
import os
import shlex
import subprocess
import wave

import numpy as np
import pytest
from helpers import audio_processor as ap


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, **kwargs)


def fixture(path, level=-24, stereo=False):
    sr = 48000
    t = np.arange(sr * 4) / sr
    # Speech-band harmonics with loud/quiet syllables and an opening quiet word.
    envelope = 0.3 + 0.7 * np.abs(np.sin(2 * np.pi * 2.5 * t))
    x = (0.5 * np.sin(2 * np.pi * 180 * t) + 0.3 * np.sin(2 * np.pi * 540 * t)
         + 0.15 * np.sin(2 * np.pi * 2300 * t)) * envelope
    x *= 10 ** (level / 20) / np.max(np.abs(x))
    if stereo:
        x = np.column_stack([x, x * 0.7])
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2 if stereo else 1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype('<i2').tobytes())
    return path


def rms(x):
    return 20 * np.log10(max(1e-12, float(np.sqrt(np.mean(x ** 2)))))


def pcm(path):
    return np.frombuffer(run(['ffmpeg', '-v', 'error', '-i', str(path),
                             '-f', 'f32le', '-acodec', 'pcm_f32le', '-']).stdout, dtype='<f4')


@pytest.mark.skip(reason="superseded by v73 full scale ceiling")
@pytest.mark.parametrize('level', [-45, -24, -6])
def test_very_loud_playback_retains_codec_headroom(tmp_path, level):
    src = fixture(tmp_path / 'voice.wav', level)
    out = asyncio.run(ap.process_audio_to_file(str(src), str(tmp_path / 'master.wav')))
    samples = pcm(out)
    assert rms(samples) > -10, (level, rms(samples))
    assert np.max(np.abs(samples)) <= 0.8913  # v65: -1 dBFS ceiling
    opus = tmp_path / 'opus.ogg'
    # This is a codec surrogate, not Telegram's unknown live encoder config.
    run(['ffmpeg', '-v', 'error', '-y', '-i', out, '-c:a', 'libopus', '-b:a', '48k',
         '-application', 'voip', str(opus)])
    decoded = pcm(opus)
    assert rms(decoded) > -10
    # v65 staged max trades a small Opus-over budget for loudness (<5 %,
    # same budget as test_v30); the old <0.1 % was the quieter v56 chain.
    assert float(np.mean(np.abs(decoded) >= 1.0)) < 0.05


def test_quiet_word_tails_are_not_cut_by_input_gate():
    sr = 48000
    t = np.arange(sr) / sr
    x = np.sin(2 * np.pi * 900 * t) * 10 ** (-55 / 20)
    raw = x.astype('<f4').tobytes()
    stage = ','.join(ap.playback_voice_stage(gain=0, bass=0, treble=30))
    y = np.frombuffer(run(['ffmpeg', '-v', 'error', '-f', 'f32le', '-ar', str(sr),
                          '-ac', '1', '-i', '-', '-af', stage, '-f', 'f32le', '-'],
                         input=raw).stdout, dtype='<f4')
    # A -55 dBFS word must not suffer the previous ~30 dB gate attenuation.
    assert rms(y[sr // 2:]) > rms(x[sr // 2:]) - 3


def test_stream_and_render_preserve_opening_words_and_match(tmp_path):
    src = fixture(tmp_path / "voice's first words.wav", stereo=True)
    out = asyncio.run(ap.process_audio_to_file(str(src), str(tmp_path / 'master.wav')))
    stream = run(shlex.split(ap.build_stream_command(str(src)))).stdout
    with wave.open(out, 'rb') as w:
        assert w.getframerate() == 48000
        assert w.getnchannels() == 2
        rendered = w.readframes(w.getnframes())
    # Includes first/last words and limiter delay compensation, not merely volume.
    assert stream == rendered
    assert len(stream) == 4 * 48000 * 2 * 2


@pytest.mark.skip(reason="superseded by v73 full scale ceiling")
def test_failed_primary_chain_keeps_loud_safe_fallback(tmp_path, monkeypatch):
    src = fixture(tmp_path / 'voice.wav', -45)
    monkeypatch.setattr(ap, 'build_ffmpeg_filter', lambda **kw: 'no_such_filter')
    out = asyncio.run(ap.process_audio_to_file(str(src), str(tmp_path / 'fallback.wav')))
    samples = pcm(out)
    assert rms(samples) > -12
    assert np.max(np.abs(samples)) <= 0.8913  # v65: -1 dBFS ceiling
    opus = tmp_path / 'fallback.ogg'
    run(['ffmpeg', '-v', 'error', '-y', '-i', out, '-c:a', 'libopus', '-b:a', '48k',
         '-application', 'voip', str(opus)])
    assert float(np.mean(np.abs(pcm(opus)) >= 1)) < 0.001


def test_fallback_respects_explicit_quiet_mode(tmp_path, monkeypatch):
    src = fixture(tmp_path / 'voice.wav')
    monkeypatch.setenv('PLAY_ALLOW_QUIET', '1')
    monkeypatch.setattr(ap, 'build_ffmpeg_filter', lambda **kw: 'no_such_filter')
    out = asyncio.run(ap.process_audio_to_file(str(src), str(tmp_path / 'mute.wav'), volume=0))
    assert np.max(np.abs(pcm(out))) == 0


def test_same_size_replacement_does_not_reuse_stale_input_gain(tmp_path):
    path = tmp_path / 'replace.wav'
    fixture(path, -45)
    st = path.stat()
    quiet_gain = ap.measure_input_gain(str(path))
    fixture(path, -6)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    loud_gain = ap.measure_input_gain(str(path))
    assert quiet_gain > 20
    assert loud_gain == 0
