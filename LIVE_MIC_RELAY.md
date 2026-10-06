# Live Mic Relay (`.mic`)

Browser mic → WebSocket → FFmpeg → py-tgcalls voice chat relay.

## Flow

```
Browser (getUserMedia, 48 kHz mono PCM16)
  → AudioWorklet/ScriptProcessor → WS binary frames (/ws/mic)
  → raw FIFO → FFmpeg (s16le, -analyzeduration 0 -probesize 32,
    filter chain: helpers.audio_processor.build_ffmpeg_filter, live=True)
  → proc FIFO → py-tgcalls MediaStream (`cat <fifo>`) → VC
```

## Token format

`user_id:chat_id:secret` — single-use, stored in DB under
`live_mic_token_{user_id}`. Burned on stop; NOT burned on browser
connect, so a dropped browser can reconnect with the same token.

## Session lifecycle rules (the bug fixes)

- **Duplicate connect**: same user+chat with a live WebSocket →
  `error:already_active`. An already-connected browser never kicks the
  VC session.
- **Reconnect within grace window** (90 s): a dropped browser with
  received audio re-attaches to the existing session — no extra
  `play()` on the py-tgcalls session, so no VC drop churn.
- **`has_live_socket(user_id)`** is the real "is mic on" check used by
  `cmd_mic` / callbacks — only true when the session is open AND the
  browser WebSocket is live.
- **Settings changes**: no-op changes are ignored; if the pipeline
  never started, settings are only remembered (no FFmpeg restart).
  Restart happens only while FFmpeg is actually running.
- **Browser JS**: auto-reconnect with exponential backoff, token generated once per page load, `rate:` only after ready
  (never settings on connect), pending PCM buffer, 20 ms AudioWorklet packets, and audioContext resume on visibilitychange.

## Deployment

- FFmpeg must be installed (Heroku ffmpeg buildpack).
- `LIVE_MIC_BASE_URL` priority: `LIVE_MIC_BASE_URL` >
  `HEROKU_APP_DEFAULT_DOMAIN_NAME` > `<app>.herokuapp.com`. The
  detected URL from `X-Forwarded-Host` (`_remember_base_url`) is
  preferred because Heroku appends a random suffix to the domain.

## Tests

`tests/test_live_mic_relay.py` — fake py-tgcalls/ntgcalls/db/session
manager with a real aiohttp WS client. Covers: session starts live and
stays live, `play()` exactly once, duplicate tab rejected, reconnect
re-attaches without extra `play()`, settings change keeps the same
session, stop burns the token and clears the mic flag, and delivered
audio is loud (≥ -3 dB mean).

## Spare ("assistant") account — required for `.mic on`

Telegram allows **one group-call join per account**. If the relay joins the
voice chat with the same account the user is listening with, Telegram drops one
of the two sessions. That was the root cause of:

* `.mic on` throwing the user out of the voice chat
* the voice chat "crashing" again and again
* no mic audio after rejoining the voice chat
* mic audio only becoming audible after leaving the voice chat

### Setup

Either set a global env var:

```
ASSISTANT_SESSION=<pyrogram session string of a SPARE account>
```

or let each user register their own spare account in the bot's private chat:

```
.micaccount <session string>   # save (message is auto-deleted)
.micaccount status             # show which spare account is in use
.micaccount off                # remove it
```

The spare account must be a member of the group (the bot auto-invites it when
it is admin). Per-user value wins over `ASSISTANT_SESSION`.

### Behaviour

* `LiveMicSession.relay` — spare account: joins the VC, plays the stream, gets
  volume-boosted, and leaves the VC on `.mic off`.
* `LiveMicSession.uvc` — the user's own account: keeps all chat state and UI
  flags, and is never pulled into the voice chat.
* With no spare account configured the old single-account path still works, and
  `.mic on` warns that the user will be kicked out of the voice chat.

Covered by `tests/test_mic_spare_account.py`.

## Link-not-coming fix (latest)

* The detected public URL is now **saved in the database** and restored on
  boot, so a dyno restart no longer breaks `.mic on` links.
* The `<app>.herokuapp.com` guess is **probed once** (`/health`); if it is not
  this bot, the link is not sent and the owner is told to set
  `LIVE_MIC_BASE_URL`. A dead link is worse than a clear error.
* Stale `mic_enabled` flags (crash / restart / dropped browser) no longer block
  `.mic on` — `reset_mic_state()` clears them and a fresh link is issued.
* `.mic off` (command and button) now clears both the relay session and the
  boost flags, so the next `.mic on` always works.


## Loudness (clean loud, not squashed)

Browser chain: clean capture by default (AEC off, browser noise suppression off,
AGC off) -> highpass 85 Hz -> lowpass 15 kHz -> mud cut 280 Hz -> presence
1.8/3.2/4.5 kHz -> de-ess 7.2 kHz -> modest pre-amp -> compressor -> make-up
gain -> **gentle** tanh safety clip (near-linear for speech, only rounds true
peaks). Server-side `afftdn` provides light denoise.

Current FFmpeg live chain: aresample 48 kHz -> highpass/lowpass and hum notch
-> light denoise -> mud cut and presence EQ -> up to 18 dB pre-amp -> speech
leveller -> two compressors -> 6 dB post-compressor makeup -> at most 5 dB of
final slider drive -> brick-wall limiter at `0.70` (about -3.1 dBFS sample
peak, leaving headroom for Telegram's Opus encode). Spare-account VC volume is
set to 200 (Telegram max).

### Why it was quiet and unclear (fixed)

The old chain stacked saturation on saturation: the browser applied a hard
`tanh(x*2.6)` shaper to *everything*, then FFmpeg added +22 dB pre-amp,
+9.5 dB EQ peaks, `acompressor makeup=10` (that is a linear x10 = +20 dB,
not 10 dB), +6, +4 and another +4 dB, an `asoftclip`, and only then the
limiter. Measured result: **-2.6 dBFS RMS for every input level**, i.e. a
permanently pinned, near-square-wave stream. A flat saturated stream reads
to the ear as muffled and *quieter*, never louder.

On top of that, the browser LOUD slider hit all four of its internal caps
at about 900%, so the 1200% default was already maxed and moving the slider
did nothing; and the AEC toggle re-applied `noiseSuppression: true` to the
live track, which muffled the voice again.

The browser's default loudness setting is 2000%; quiet words are levelled
forward, while the server caps peaks before the Telegram encode.

Measured with the current server filter and a 440 Hz test tone at the stated
input peak (not a recording of a physical phone mic):

| Input peak | Output peak | Output RMS |
|---|---|---|
| -20 dBFS | -3.1 dBFS | -11.2 dBFS |
| -35 dBFS | -3.1 dBFS | -11.2 dBFS |
| -50 dBFS | -3.1 dBFS | -11.8 dBFS |

### Mic page controls (browser side, apply instantly, no FFmpeg restart)
| Control | Range | Default | Effect |
|---|---|---|---|
| LOUD | 100-2000 % | 2000 % | browser capture level |
| CLARITY | 0-35 | 35 | presence + consonant lift with a matching mud cut |

Both are saved in `localStorage` and re-applied the next time the mic starts.
Browser-only controls never restart FFmpeg. Explicit preset/reset actions may
send server settings, but saved settings are no longer pushed automatically a
few seconds after connect (that used to create a gap in the live stream).

### Anti-stutter
Adaptive jitter buffer: starts at 180 ms, grows +60 ms per underrun up to
420 ms, holds up to 1.2 s, trims latency gently (a few samples every 200 ms).
The browser emits one 20 ms PCM packet at the device's actual sample rate,
matching the server pacer instead of drifting with fixed 1024-sample packets.

## Which account does what

- Main account (logged in): sends commands, stays in VC; not the voice.
- Spare account (`.micaccount <session>` in bot DM): streams your live voice. Must be a group member.
- Phone browser: opens the link from `.mic on`, captures the mic.

## Private (DM) use — group mein kuch mat likho
1. Bot DM: `.mic chat -100xxxxxxxx` (ya @username / invite link) — ek baar.
2. Bot DM: `.mic on` → link aayega, kholo, bolo. Aawaz usi group ki VC mein.
3. Bot DM: `.mic off` → band.
Group mein `.mic on` likha to bot command message turant delete kar deta hai.

## Telegram mic icon
- `.mic mute` → spare account ka TG mic OFF (aawaz nahi jaati).
- `.mic unmute` → TG mic ON. `.mic toggle` → ulta kar deta hai.
- `.mic on` par mic apne aap ON hota hai, `.mic off` par account VC chhod deta hai.
- Mic icon OFF rakh ke aawaz bhejna Telegram allow nahi karta — muted account ki aawaz koi nahi sunta.

## Clarity
See "Mic page controls" above (LOUD + CLARITY sliders on the mic page).

## Live = playback loudness (latest fix)

Problem: a played audio file sounded strong in the VC, the live mic did not.

Cause: the two paths used **different** FFmpeg chains. Playback ran the big
chain (+20 dB -> 3 compressors -> extra gain -> hard soft-clip -> limiter,
plus loudnorm); the live mic ran a separate gentle chain, and on top of that
the phone squashed the voice before sending it (x7 pre-amp, hard compressor,
tanh saturation), so the server received an already-flat signal it could never
un-flatten.

Fix:
* The live mic now runs **the same chain as playback**, with only the two
  impossible-live filters swapped for low-latency equivalents:
  37 s gaussian `dynaudnorm` -> short-window `dynaudnorm`, and 3 s `loudnorm`
  -> `speechnorm=e=25`. Playback itself is untouched.
* The browser is now a **clean capture**: shaping EQ, a modest 1.6x pre-amp, a
  peak-catching compressor (threshold -10 dB, ratio 4) and a near-transparent
  soft clip, only so the 16-bit conversion never hard-clips. LOUD/CLARITY
  sliders now trim this clean signal instead of squashing it.
* Escape hatch: `LIVE_MIC_CHAIN=soft` restores the previous gentle live chain.

Measured (sine into the chain, `volumedetect`):

| Input | Live (new) | Live (old) | Playback |
|---|---|---|---|
| -20 dBFS | -0.1 dB | -1.8 dB | -0.2 dB |
| -35 dBFS | -0.1 dB | -1.5 dB | -0.2 dB |
| -50 dBFS | -0.1 dB | -2.0 dB | -0.2 dB |
| silence | -91 dB (silent) | | |

## 28-Sep audio rework (historical; superseded by the current chain above)

The live chain used to stack ~90 dB of gain (30 dB pre-amp, two compressors
with +14 dB makeup, +12 dB EQ boosts, +14 dB drive, +18 dB turbo, saturator,
two limiters) into a 0 dBFS ceiling. The limiters were flat out constantly, so
the voice lost all dynamics and consonants — it sounded *quieter* and muddier,
not louder.

The measurements and chain below are from the earlier 28-Sep tuning pass;
they are retained as history and are not the current live-mic settings:

```
aresample 48k -> highpass 90 -> lowpass 15k -> hum notch -> afftdn nr=12
-> mud cut 300Hz -> presence 1.8k/3k/4.8k -> de-ess 7.6k
-> pre-amp 10 dB -> speechnorm e=12.5 -> acompressor 3:1 makeup 4
-> drive <= 6 dB -> alimiter limit=0.94
```

Measured: a -28 dBFS input lands at **-11.8 LUFS with -3.6 dBFS true peak** —
loud, and with the headroom Opus needs to avoid its own crackle.

Browser capture: `autoGainControl` is **off** (it pumped against the server
leveller), `noiseSuppression` is **off** by default to preserve consonants, and
the server's light `afftdn` handles hiss. The **NUCLEAR MAX TEST (Default)**
profile uses browser loud 2000%, clarity 35, zero added bass, server gain 400,
24 turbo, and 18 dB pre-amp. It is intentionally loudness-first and can sound
flat/harsh; it exists to establish the maximum practical level before tuning
back toward clean dynamics.
