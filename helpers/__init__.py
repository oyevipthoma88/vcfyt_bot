"""Activate playback-only improvements and Live Mic audio processing."""
from .playback_v56 import install as _install_playback
from .playback_v57 import install as _install_playback_v57

_install_playback()
_install_playback_v57()
from .playback_v58 import install as _install_playback_v58

_install_playback_v58()
# v59 (last): loud + clear - stops lifting the hiss floor that buried the voice.
from .playback_v59 import install as _install_playback_v59

_install_playback_v59()
# v60 (last): fight chain restored - presence + peak compressor, Opus-safe.
from .playback_v60 import install as _install_playback_v60

_install_playback_v60()
# v61 (last): ULTRA fight chain - -1.5 dBFS ceiling, de-box EQ, exciter,
# double peak catch around the clipper: ~+3.6 LU louder after Opus.
from .playback_v61 import install as _install_playback_v61

_install_playback_v61()

# v62: ULTRA PRO MAX fight chain (multiband, Opus-safe ceiling)
from .playback_v62 import install as _install_playback_v62
_install_playback_v62()

# v63 (last): REAL MAX - -0.3 dBFS ceiling, no level-eating stages after the
# drive; fixes "code me max, VC me normal".
from .playback_v63 import install as _install_playback_v63
_install_playback_v63()

# v64 (last): FIGHT LOUD - drive floor 20 dB at max boost; saved .playdb can
# only push higher. Fixes "normal audio music bot ke barabar".
from .playback_v64 import install as _install_playback_v64
_install_playback_v64()

# v65 (last): STAGED MAX - 3 push stages with limiters between (~56 dB),
# -1 dBFS ceiling: louder than v64 and less square/crackle.
from .playback_v65 import install as _install_playback_v65
_install_playback_v65()

# v66 (last): BHARI + CLEAR + LOUD - bass body back (lowshelf 100 Hz),
# no 10 kHz muffle, 2 clean push stages instead of 56 dB into a clipper.
from .playback_v66 import install as _install_playback_v66
_install_playback_v66()

# v68 (last): TEZ + CLEAR - one push into an oversampled -1 dBTP limiter
# instead of ~47 dB through 3 limiters + clipper (that made a square wave:
# loudness == peak, "dabi" sound).  -7..-8 LUFS after Opus, real dynamics.
from .playback_v68 import install as _install_playback_v68
_install_playback_v68()

# v69 (last): MAX TEZ + CLEAR - presence EQ + hotter push into a -0.35 dBFS
# oversampled limiter: ~-5 LUFS after Opus (v68 was -7..-8), still no clipper.
from .playback_v69 import install as _install_playback_v69
_install_playback_v69()

# v71 (last): ANTI-DUCK - 150 Hz low-cut, voice bite EQ, dense comp + soft
# clip, mono-coherent, -1 dBFS ceiling: opponent ke boosted mic ke upar bhi
# aawaz dabti nahi.
from .playback_v71 import install as _install_playback_v71
_install_playback_v71()

# v73 (last): FULL SCALE - 0.00 dBFS ceiling (v71 was hard-locked at -1 dBFS),
# +18..24 dB push into two oversampled soft clippers: max RMS density.
from .playback_v73 import install as _install_playback_v73
_install_playback_v73()

# v74 (last): DAMDAR BASS + chhoti awaaz lift - bass/treble/gain settings
# work again, psychoacoustic bass harmonics (phone pe bhi bass), 40 Hz cut
# instead of 150 Hz, speechnorm lifts the softest sounds.
from .playback_v74 import install as _install_playback_v74
_install_playback_v74()

# v75 (last): AUR TEZ - +6 dB extra drive into the final clipper and
# FFmpeg virtualbass on top of v74 (env PLAY_EXTRA_DRIVE_DB=0..12).
from .playback_v75 import install as _install_playback_v75
_install_playback_v75()

# v77 (last): PHONE-MAX - played files put all energy in the 300 Hz-6 kHz
# band phones actually play (+3.2 dB louder on phone, less clipping).
# `.loudmode bass` / env PLAY_LOUD_MODE=bass brings the v75 chain back.
from .playback_v77 import install as _install_playback_v77
_install_playback_v77()

# v79: LIVE-LOUDEST live mic chain (phone band, hard levelling, tanh drive).
from .live_mic_v79 import install as _install_live_mic_v79
_install_live_mic_v79()
