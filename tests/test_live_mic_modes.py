import re
import shutil
import subprocess

import numpy as np
import pytest

from helpers import live_mic, live_mic_v79


def test_three_server_modes_have_safe_limiter_and_distinct_filters():
    chains = {mode: live_mic_v79.build_live_mic_filter(mode=mode)
              for mode in live_mic_v79.MODES}
    assert set(chains) == {"loudest", "clean", "warm"}
    for chain in chains.values():
        assert "alimiter=" in chain
        assert "limit=" in chain
    assert "limit=0.7499" in chains["loudest"]
    assert "limit=0.7499" in chains["clean"]
    assert "limit=0.7499" in chains["warm"]
    assert "volume=20dB" in chains["warm"]
    assert "speechnorm=e=25" in chains["loudest"]
    assert chains["clean"] != chains["loudest"]
    assert chains["warm"] != chains["clean"]


def test_browser_modes_are_session_controls_without_reload():
    page = live_mic.LIVE_MIC_HTML
    assert "setSoundMode('loudest')" in page
    assert "setSoundMode('clean')" in page
    assert "setSoundMode('warm')" in page
    assert "sendControl('mode',{mode})" in page
    assert "Mode badalne ke liye page reload hoga" not in page
    assert 'id="boostR"' in page


def test_each_mode_runs_through_ffmpeg_opus_with_bounded_peaks():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    sr=48000; t=np.arange(sr*3,dtype=np.float64)/sr
    env=np.maximum(0, np.sin(2*np.pi*3*t))**1.5
    x=(0.23*env*np.sin(2*np.pi*170*t)+0.09*env*np.sin(2*np.pi*510*t)+
       0.035*env*np.sin(2*np.pi*1700*t)).astype(np.float32)
    pcm=(np.clip(x,-1,1)*32767).astype(np.int16).tobytes()
    results={}
    for mode in live_mic_v79.MODES:
        filt=live_mic_v79.build_live_mic_filter(mode=mode)
        proc=subprocess.run(["ffmpeg","-v","error","-f","s16le","-ar",str(sr),"-ac","1","-i","-",
                             "-af",filt,"-c:a","libopus","-b:a","48k","-f","ogg","-"],
                            input=pcm,capture_output=True)
        assert proc.returncode==0, proc.stderr.decode()[-500:]
        dec=subprocess.run(["ffmpeg","-v","error","-i","-","-f","s16le","-ar",str(sr),"-ac","1","-"],
                           input=proc.stdout,capture_output=True,check=True).stdout
        out=np.frombuffer(dec,np.int16).astype(np.float64)/32768
        results[mode]=out
        assert np.isfinite(out).all()
        assert np.mean(np.abs(out)>=0.999) < 0.002
        assert np.max(np.abs(out)) <= 1.0
    assert any(not np.array_equal(results["loudest"],results[m]) for m in ("clean","warm"))
