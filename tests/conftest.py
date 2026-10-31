"""Pytest setup.

Some test files are standalone scripts (``python tests/<file>.py``) that
replace ``helpers.*`` / ``pyrogram`` in ``sys.modules`` with fakes at import
time.  Collected inside one pytest process they poisoned every later test
(``cannot import name 'Database' from 'helpers.database'``).  They are run
in their own subprocess by ``test_standalone_scripts.py`` instead.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STANDALONE_SCRIPTS = [
    "test_live_mic_relay.py",
    "test_mic_spare_account.py",
    "test_live_mic_audio_quality.py",
    "verify_audio_pipeline.py",
]

collect_ignore = list(STANDALONE_SCRIPTS)
