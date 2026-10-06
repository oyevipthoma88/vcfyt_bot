# Upstream API verification

The current PyTgCalls public repository and documentation were checked on 2026-09-01.

## Sources

1. https://github.com/pytgcalls/pytgcalls — upstream PyTgCalls repository. Its README describes `play`, pause/resume, stop/play, volume control, support for Pyrogram/Telethon/Hydrogram, and Python 3.10+.
2. https://tgcalls.org/implementation/group_call_raw.html — API documentation. It documents pause/resume/stop playout and `edit_group_call_member`, which edits a participant's volume and requires voice-chat management permission.
3. https://pypi.org/project/py-tgcalls/ — PyPI metadata checked for `py-tgcalls` 2.3.3. The package description confirms the voice-chat transport features and current Python compatibility.

## Implementation implications

The repository's `PyTgCalls` transport methods (`play`, `pause`, `resume`, and `leave_call`) are consistent with the supported feature set. Telegram participant-volume control is a server-side participant setting and is separate from FFmpeg playback loudness. The bot therefore only claims to apply the logged-in account's saved participant volume, not to change every participant or defeat an administrator mute.

The bot's playback audio continues to use a real FFmpeg chain with loudness normalization, EQ, compression, user-controlled dB gain, and a final limiter. No arbitrary 1000x/100 dB claim is treated as real loudness.
