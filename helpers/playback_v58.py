"""Playback v58: louder to the ear in Telegram VC (presence tilt).

A real VC recording (user test, record_29) was already -8 LUFS but sounded
"kam": its 1-4 kHz band (where the ear / phone speakers hear loudness) sat
~17 dB under the 150-500 Hz boom.  The limiter ceiling is fixed, so bass
boom was wasting the headroom.  This stage, placed before the fight
compressor, removes rumble/boom and pushes voice presence: +3.5 dB in
1-4 kHz after Opus at the same crackle level.
"""
from . import audio_processor as ap

PRESENCE_EQ = [
    "highpass=f=160:p=2",                       # rumble: eats headroom, inaudible on phones
    "equalizer=f=300:t=q:w=0.9:g=-6",           # boom / mud
    "equalizer=f=2200:t=q:w=1.4:g=9",           # voice presence = perceived loudness
    "highshelf=f=4500:g=4",                     # bite / consonants cut through the other mic
]

_v56_fight = None


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    return PRESENCE_EQ + _v56_fight(bass=bass, boost=boost, over=over)


def install():
    global _v56_fight
    if getattr(ap, "_playback_v58_installed", False):
        return
    ap._playback_v58_installed = True
    _v56_fight = ap.playback_fight_stage
    ap.playback_fight_stage = playback_fight_stage
