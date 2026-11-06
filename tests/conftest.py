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


import pytest


@pytest.fixture(autouse=True)
def _legacy_live_mic_chain(request):
    """Older tests pin the pre-v79 live mic chain; v79 tests set their own."""
    if "test_v79" in request.node.nodeid:
        yield
        return
    try:
        from helpers import live_mic_v79 as v79
    except Exception:
        yield
        return
    old = v79._mode
    v79._mode = "clean"
    yield
    v79._mode = old
