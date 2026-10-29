"""Playback-only improvements, activated by helpers package initialization.

FFmpeg: https://ffmpeg.org/ffmpeg-filters.html#alimiter
https://ffmpeg.org/ffmpeg-filters.html#acompressor
Opus: https://opus-codec.org/docs/opus_api-1.5/group__opus__decoder.html
Receiver-side Telegram suppression is not established by local codec tests.
"""
import asyncio
import logging
import math
import os
import re
import shlex
import subprocess
import tempfile
from typing import Optional
from . import audio_processor as ap
log = logging.getLogger(__name__)

def measure_input_gain(path: str) -> float:
    if not path or not os.path.exists(path):
        return 0.0
    try:
        key = (os.path.abspath(path), os.path.getsize(path), os.stat(path).st_mtime_ns, os.stat(path).st_ctime_ns)
    except OSError:
        return 0.0
    if key in ap._INPUT_GAIN_CACHE:
        return ap._INPUT_GAIN_CACHE[key]
    gain = 0.0
    try:
        import subprocess
        out = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-t', '90', '-i', path, '-vn', '-sn', '-dn', '-af', 'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,highpass=f=70:p=2,highpass=f=70:p=2,ebur128=peak=sample', '-f', 'null', '-'], capture_output=True, text=True, timeout=30).stderr
        lufs = re.findall('I:\\s+(-?[\\d.]+|-inf) LUFS', out)
        peak = re.findall('Peak:\\s+(-?[\\d.]+|-inf) dBFS', out)
        if lufs and lufs[-1] != '-inf' and (float(lufs[-1]) > -70):
            gain = ap.INPUT_TARGET_LUFS - float(lufs[-1])
            if peak and peak[-1] != '-inf':
                gain = min(gain, -1.0 - float(peak[-1]))
            gain = max(0.0, min(ap.INPUT_MAX_GAIN_DB, gain))
    except Exception:
        gain = 0.0
    if len(ap._INPUT_GAIN_CACHE) > 256:
        ap._INPUT_GAIN_CACHE.clear()
    ap._INPUT_GAIN_CACHE[key] = gain
    return gain

def playback_voice_stage(bass: int=None, treble: int=None, gain: int=None, input_gain_db: float=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f'volume={ap._db(input_gain_db)}dB')
    if ap._has_filter('afftdn'):
        out.append('afftdn=nr=18:nf=-55:tn=0')
    if ap._has_filter('agate'):
        out.append('agate=threshold=0.00056:range=0.25:ratio=1.5:attack=3:release=300:knee=6:detection=rms')
    g = ap.GAIN_MAX if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    pre = 12.0 * g / float(ap.GAIN_MAX)
    if pre > 0.05:
        out += [f'volume={ap._db(pre)}dB', 'alimiter=level_in=1:level_out=1:limit=0.9:attack=2:release=50:level=false']
    b = ap.clamp(14 if bass is None else int(bass), ap.BASS_MIN, ap.BASS_MAX)
    if b:
        out.append(f'equalizer=f=120:t=q:w=1.1:g={min(10.0, b * 0.3):.1f}')
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    tg = (t - 30) * 0.045
    if abs(tg) >= 0.1:
        out.append(f'highshelf=f=3500:g={tg:.1f}')
    return out

def playback_fight_stage(bass: int=None, boost: int=None, over: float=1.0) -> list:
    b = max(0, min(40, 14 if bass is None else int(bass)))
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    out = [f'equalizer=f=95:t=q:w=1.1:g={min(8.0, b * 0.45):.1f}', 'equalizer=f=400:t=q:w=1:g=-3', 'equalizer=f=1500:t=q:w=1:g=3', 'equalizer=f=2700:t=q:w=1.1:g=6', 'equalizer=f=4000:t=q:w=1.2:g=3']
    if ap._has_filter('speechnorm'):
        out.append('speechnorm=e=25:r=0.0008:l=1:p=0.9')
    else:
        out.append('dynaudnorm=f=150:g=15:p=0.9:m=30')
    out.append(f'acompressor=threshold=0.09:ratio=4:attack=6:release=120:detection=rms:makeup={3.0 + bo * 0.15:.1f}:knee=4')
    hp = int(ap._env_db('PLAY_HP_FREQ', 120.0, high=220.0, low=80.0))
    drive = ap._env_db('PLAY_DRIVE_DB', 16.0 + 0.4 * bo, high=24.0, low=0.0)
    out += [f'highpass=f={hp}:p=2', f'volume={ap._db(drive)}dB']
    if ap._has_filter('asoftclip'):
        out.append('asoftclip=type=tanh:oversample=4')
    out += ['lowpass=f=11000:p=2', 'volume=3dB']
    return out

def playback_ceiling() -> list:
    lim = min(ap.PLAY_CEILING_SAFE, 10 ** (ap._env_db('PLAY_CEILING_DB', -1.5, high=-0.5, low=-3.0) / 20.0))
    return ['aresample=192000', f'alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=5:release=100:level=false:latency=true', 'aresample=48000', f'alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=50:level=false:latency=true']

def build_ffmpeg_filter(volume: int=None, bass: int=None, echo: bool=None, echo_level: int=None, boost: int=None, relay_volume: int=None, gain: int=None, treble: int=None, extra_filters: str='', live: bool=False, pregain: float=None, turbo: float=None, clarity: float=None, stream: bool=False, input_gain_db: float=0.0) -> str:
    if live:
        return ap.build_live_mic_filter()
    v = 1000 if volume is None else max(0, int(volume))
    if os.environ.get('PLAY_ALLOW_QUIET', '0') != '1':
        v = max(v, ap.VOLUME_MAX)
    over = max(0.0, min(1.0, (v - ap.VOLUME_CLEAN_MAX) / float(ap.VOLUME_MAX - ap.VOLUME_CLEAN_MAX)))
    filters = ['aresample=48000', 'aformat=sample_fmts=fltp:channel_layouts=stereo', 'highpass=f=70:p=2', 'highpass=f=70:p=2']
    filters += ap.playback_voice_stage(bass=bass, treble=treble, gain=gain, input_gain_db=input_gain_db)
    _el = max(0, min(ap.LEVEL_MAX, int(echo_level if echo_level is not None else 0)))
    if echo and _el:
        filters.append(f'aecho=0.8:0.85:{40 + _el * 15}:{0.12 + _el * 0.04:.2f}')
    filters += ap.playback_fight_stage(bass=bass, boost=boost, over=over)
    filters += ap.playback_ceiling()
    if extra_filters:
        m = re.match('^volume=(\\d+(?:\\.\\d+)?)dB$', extra_filters.strip())
        if m:
            vols = [i for i, x in enumerate(filters) if x.startswith('volume=')]
            idx = vols[-2] if len(vols) >= 2 else len(filters) - 1
            filters.insert(idx, f'volume={min(6.0, float(m.group(1)) / 3):.2f}dB')
    if v == 0:
        filters.append('volume=0')
    elif v < ap.VOLUME_CLEAN_MAX:
        import math as _m
        filters.append(f'volume={20 * _m.log10(v / ap.VOLUME_CLEAN_MAX):.2f}dB')
    return ','.join(filters)

def fallback_filter(volume=None, input_gain_db=0.0, extra_filters=''):
    extra = re.fullmatch('volume=(\\d+(?:\\.\\d+)?)dB', extra_filters.strip())
    push = min(6.0, float(extra.group(1)) / 3) if extra else 0.0
    parts = ['aresample=48000', 'aformat=sample_fmts=fltp:channel_layouts=stereo', 'highpass=f=70:p=2', 'highpass=f=70:p=2', f'volume={max(0.0, min(40.0, input_gain_db)):.2f}dB', 'dynaudnorm=f=150:g=15:p=0.9:m=30', 'acompressor=threshold=0.09:ratio=4:attack=6:release=120:makeup=4:knee=4:detection=rms', f'volume={20 + push:.2f}dB'] + ap.playback_ceiling()
    if os.environ.get('PLAY_ALLOW_QUIET', '0') == '1':
        v = 1000 if volume is None else max(0, int(volume))
        if v == 0:
            parts.append('volume=0')
        elif v < 1000:
            parts.append(f'volume={20 * math.log10(v / 1000):.2f}dB')
    return ','.join(parts)

async def process_audio_to_file(input_path: str, output_path: Optional[str]=None, volume: int=None, bass: int=None, echo: bool=None, echo_level: int=None, boost: int=None, relay_volume: int=None, gain: int=None, treble: int=None, extra_filters: str='') -> str:
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix='.wav', prefix='vc_processed_')
        os.close(fd)
    af = ap._sanitize_ffmpeg_filter(ap.build_ffmpeg_filter(volume=volume, bass=bass, echo=echo, echo_level=echo_level, boost=boost, relay_volume=relay_volume, gain=gain, treble=treble, extra_filters=extra_filters, input_gain_db=await asyncio.to_thread(ap.measure_input_gain, input_path)))

    async def _run(filter_chain: str) -> tuple:
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', input_path, '-vn', '-af', filter_chain, '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', output_path]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        return (proc.returncode, err)
    code, stderr = await _run(af)
    if code != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) <= 44:
        log.warning('Playback primary chain failed; using safe fallback: %s', stderr.decode(errors='replace')[-300:])
        fallback = ap.fallback_filter(volume=volume, extra_filters=extra_filters, input_gain_db=await asyncio.to_thread(ap.measure_input_gain, input_path))
        code2, stderr2 = await _run(fallback)
        if code2 != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) <= 44:
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise RuntimeError(f"FFmpeg failed: {stderr.decode(errors='replace')[-300:]} | fallback: {stderr2.decode(errors='replace')[-300:]}")
    return output_path

def build_stream_command(input_path: str, volume: int=None, bass: int=None, echo: bool=None, echo_level: int=None, boost: int=None, relay_volume: int=None, gain: int=None, treble: int=None, extra_filters: str='', loop: bool=False, start_at: float=0.0) -> str:
    import shlex
    af = ap._sanitize_ffmpeg_filter(ap.build_ffmpeg_filter(volume=volume, bass=bass, echo=echo, echo_level=echo_level, boost=boost, relay_volume=relay_volume, gain=gain, treble=treble, extra_filters=extra_filters, stream=True, input_gain_db=ap.measure_input_gain(input_path)))
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1']
    if loop:
        cmd += ['-stream_loop', '-1']
    if start_at and start_at > 0:
        cmd += ['-ss', f'{start_at:.2f}']
    cmd += ['-i', input_path, '-vn', '-sn', '-dn', '-af', af, '-ar', '48000', '-ac', '2', '-f', 's16le', '-flush_packets', '1', 'pipe:1']
    return shlex.join(cmd)

def install():
    """Keep all existing import paths and leave live/bridge DSP untouched."""
    if getattr(ap, "_playback_v56_installed", False):
        return
    ap._playback_v56_installed = True
    for name in ("measure_input_gain", "playback_voice_stage", "playback_fight_stage",
                 "playback_ceiling", "build_ffmpeg_filter", "fallback_filter",
                 "process_audio_to_file", "build_stream_command"):
        setattr(ap, name, globals()[name])
