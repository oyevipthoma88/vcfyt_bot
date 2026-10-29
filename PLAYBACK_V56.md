# Playback v56

Five targeted improvements after tracing command -> PCM -> PyTgCalls:

1. Finalize 48 kHz stereo float layout before measurement and mastering; use nanosecond timestamps and ctime to avoid stale gain after same-size file replacements.
2. Preserve quiet word tails with a gentle -65 dB expander instead of the old -48 dB gate with up to 30 dB attenuation. Fixed-floor noise reduction (no adaptive voice learning) controls the resulting hiss lift.
3. Use smoother RMS 4:1 compression (6 ms attack, 120 ms release) instead of 7:1 / 2 ms / 60 ms. Remove the continuously-driven 0.6 density limiter; retain one band-limited warmth stage.
4. Oversample output limiting to 192 kHz, disable automatic makeup, compensate lookahead delay, and guard sample peaks again after downsampling. Maintain 0.84 amplitude ceiling (~-1.5 dBFS).
5. Preserve opening packets and quiet first words by removing file-input nobuffer/tiny probes/silence trimming; quote command arguments safely. Render fallback keeps the same output ceiling and explicit mute semantics rather than switching to 0 dBFS.

Implemented in helpers/playback_v56.py, activated once through helpers/__init__.py; existing audio import paths remain valid. Live-mic and bridge functions are unchanged. The original audio_processor remains available for shared helpers.

## Validation

Run each relevant file independently if other legacy tests inject module stubs:

    python -m pytest -q tests/test_v56_playback_quality.py
    python -m pytest -q tests/test_v54_fight_chain_play.py
    python -m pytest -q tests/test_v52_quiet_audio_and_ss.py -k 'not ss_off'
    python -m pytest -q tests/test_loudness_controls.py -k 'not live_mic'

19 relevant tests passed; 2 historical playback checks are skipped by their pre-existing decorators. New active tests cover output loudness, Opus round-trip clipping, quiet word retention, exact stream/render timing and PCM parity, unsafe fallback, explicit fallback mute, and stale gain invalidation.

Local speech comparison on the first 20 seconds of FFmpeg's talk109-q5.spx, stereo output: v55 -5.6 LUFS / -0.9 dBTP, v56 -5.3 LUFS / -1.5 dBTP. This is one sample, not a guarantee for every recording. Synthetic quiet/normal/loud sources retained less than 0.1% clipped samples after 48 kb/s voip Opus round-trip. Actual Telegram encoding settings were not read.

## Limits and rollout

Push is not deployment: restart/redeploy the bot server to load these Python changes. No live Telegram VC receiver recording was available, so perceived clarity or absence of Telegram-side suppression is not verified. Participant 200% volume behavior is unchanged and can cause additional receiver-side gain/clipping. Start listening at a low device volume; the mastering level is aggressive. Source recordings already clipped/noisy cannot be fully restored by extra gain. Preserve raw original recordings for listening comparisons.

Do not add more gain or limiters merely to reach 0 dBFS. Oversampling is not a certified guarantee of every decoded true peak. Two-pass loudnorm and multiband processing were considered but not added: forced dynamic normalization or unnecessary crossovers would change this speech-focused pipeline without evidence of a benefit.

References:
- https://ffmpeg.org/ffmpeg-filters.html#alimiter
- https://ffmpeg.org/ffmpeg-filters.html#acompressor
- https://ffmpeg.org/ffmpeg-filters.html#afftdn
- https://ffmpeg.org/ffmpeg-filters.html#loudnorm
- https://opus-codec.org/docs/opus_api-1.5/group__opus__decoder.html
