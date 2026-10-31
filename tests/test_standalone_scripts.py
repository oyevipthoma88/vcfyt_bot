"""Run the standalone script tests, each in a fresh interpreter."""
import os
import subprocess
import sys

import pytest

from conftest import ROOT, STANDALONE_SCRIPTS


@pytest.mark.parametrize("script", STANDALONE_SCRIPTS)
def test_standalone_script_passes(script):
    path = os.path.join(ROOT, "tests", script)
    p = subprocess.run([sys.executable, path], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    out = (p.stdout + p.stderr)[-3000:]
    assert p.returncode == 0, out
    assert "FAIL" not in p.stdout.replace("FAILED: []", ""), out
