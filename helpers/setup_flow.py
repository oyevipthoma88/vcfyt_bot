"""Guided Live Mic setup: agla step saved data se nikalta hai, kabhi skip nahi."""


def mic_setup_step(logged, spare, source, target) -> str:
    if not logged:
        return "login"
    if not spare:
        return "spare"
    if not source:
        return "src"
    if not target:
        return "tgt"
    return "ready"
