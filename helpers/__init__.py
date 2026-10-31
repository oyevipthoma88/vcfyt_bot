"""Activate playback-only improvements without changing live/bridge audio."""
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
