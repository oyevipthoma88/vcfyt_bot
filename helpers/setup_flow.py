"""Guided setup steps for browser-based Live Mic."""

def mic_setup_step(logged, spare, source, target) -> str:
    if not logged:
        return "login"
    if not target:
        return "tgt"
    return "ready"
