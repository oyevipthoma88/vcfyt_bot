# VC Fyt Bot

Multi-user Telegram **Voice Chat audio bot** with in-bot login, button-driven controls, loud and clear playback, live voice boost, queue, tags, and logging. The bot is built with **Pyrofork, PyTgCalls, FFmpeg, and yt-dlp**.

> **Important:** This bot needs a Telegram user session to join voice chats. The bot account handles commands, while the logged-in user account is the voice-chat participant.

## Features

| Feature | What it does |
|---|---|
| In-bot login | Phone + OTP + optional 2FA flow, or a String Session from the session generator. |
| Button-first interface | `/start`, `/help`, settings, tutorial, playback, and audio controls are accessible through inline buttons. |
| Loud and clear playback | FFmpeg speech normalization, presence EQ, compression, loudnorm, and a safety limiter are applied to recordings and downloaded audio. |
| Live participant volume | On playback and reconnect, the bot attempts to apply the logged-in account’s saved Telegram participant volume up to `20000` / `200%`. |
| Queue and tags | Play immediately, add to queue, loop tracks, and save reusable audio tags. |
| Audio library | Users can save My Audio; owner-saved files appear for everyone under Bot Audios. |
| Multi-user sessions | Each owner gets an isolated Pyrogram + PyTgCalls engine and separate settings. |
| Participant volume | The logged-in voice-chat account can be set to Telegram’s supported participant-volume maximum; other participants are not modified. |
| Logging | Login, VC activity, commands, boosts, errors, and broadcasts can be sent to the configured log channel. |

## One-click Heroku deployment

<a href="https://heroku.com/deploy?template=https://github.com/oyevipthoma88/vcfyt_bot"><img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy to Heroku"></a>

Click the button, choose an app name, enter the required Telegram values, and click **Deploy app**. After deployment, open **Resources** and turn on the `worker` dyno. The included `app.json` configures the Python and FFmpeg buildpacks automatically.

Heroku deployment is best paired with MongoDB because the dyno filesystem is not intended for permanent SQLite data. Add `MONGO_URI` before first use if you want sessions and settings to persist safely across dyno restarts.

## Required Telegram setup

Create `API_ID` and `API_HASH` at [my.telegram.org](https://my.telegram.org). Create a bot token using [@BotFather](https://t.me/BotFather). Find your numeric Telegram owner ID with [@userinfobot](https://t.me/userinfobot). Add the bot to the log channel as an administrator if you want logging enabled. The logged-in user account must be a member of every group where it will join a voice chat.

## Local or VPS installation

```bash
git clone https://github.com/oyevipthoma88/vcfyt_bot.git
cd vcfyt_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Telegram values
python main.py
```

Install **FFmpeg** and **yt-dlp** on the host before starting the bot. On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg
pip install -U yt-dlp
```

## Configuration

| Variable | Required | Default | Purpose |
|---|---:|---:|---|
| `API_ID` / `API_HASH` | Yes | — | Telegram application credentials. |
| `BOT_TOKEN` | Yes | — | Token from @BotFather. |
| `OWNER_ID` or `OWNER_IDS` | Yes* | — | Primary/additional numeric owner IDs. |
| `LOG_CHANNEL` | No | `-1004303404961` | Channel for operational logs. |
| `STRING_SESSION` | No | — | Optional owner session; users can also use `/login`. |
| `MONGO_URI` | Recommended on Heroku | SQLite | Persistent MongoDB connection URI. |
| `DEFAULT_BOOST` | No | `9` | Loudness stage from `0` to `10`. |
| `RELAY_DEFAULT_VOLUME` | No | `1000` | Playback volume from `0` to `1000`; mapped to a real `-12 dB` to `+24 dB` FFmpeg range. |
| `RELAY_DEFAULT_GAIN` | No | `150` | Playback gain from `0` to `150`; mapped to a real `-6 dB` to `+18 dB` FFmpeg range. |
| `RELAY_DEFAULT_BASS` | No | `8` | Controlled low-end lift from `0` to `100`. |
| `RELAY_DEFAULT_TREBLE` | No | `62` | Presence/clarity control from `0` to `100`. |
| `DEFAULT_ECHO` | No | `false` | Keep off for maximum speech clarity. |
| `DEFAULT_ECHO_LEVEL` | No | `2` | Echo intensity from `0` to `10`. |
| `LIVE_BOOST_DEFAULT` | No | `20000` | Logged-in account participant volume; Telegram maximum is `20000`. |
| `START_PIC` | No | — | Telegram file ID or public image URL shown on `/start`. |
| `AUTO_LIVE_BOOST` | No | `true` | Re-apply live boost after joins and reconnects. |

`OWNER_ID` is kept for backward compatibility. For multiple owners, use `OWNER_IDS=123456789,987654321`.

## First-use tutorial

1. Send `/start` to the bot and open **Tutorial**.
2. Open **Login** and complete phone, OTP, and optional 2FA steps, or send a String Session.
3. Add the logged-in account to the target group and start a Telegram Voice Chat.
4. Reply to an audio/video message with `.play`, or send `.play <YouTube/SoundCloud URL>`.
5. Use **Audio Settings** or `.max` when you need stronger playback. Volume now uses a practical `0–1000` scale; `+25` and `+100` buttons produce real incremental FFmpeg dB changes. Use `.pause`, `.resume`, `.skip`, `.queue`, and `.stop` for transport controls. The playback message also has **Now Playing**, **Reset Audio**, and **Auto** controls.
6. Reply to an audio/video message with `.saveaudio <title>` for My Audio. The owner can use `/addaudio <title>`; those files appear for every user under Bot Audios.
7. Use `.myboost 20000` for the logged-in account's live participant volume. The bot attempts to re-apply this value when the account joins or reconnects.

## Command reference

| Category | Commands |
|---|---|
| Account | `/start`, `/login`, `/addstring`, `/logout`, `/mystatus`, `/settings`, `/help` |
| Playback | `.play`, `.padd`, `.playforce`, `.fplay`, `.loop`, `.pause`, `.resume`, `.skip`, `.stop`, `.end`, `.leave`, `.queue`, `.vcinfo` |
| Tags | `.tag <name>`, `.untag <name>`, `.tags` |
| Audio | `/volume <0-1000>` (0 = -12 dB, 1000 = +24 dB), `/gain <0-150>` (0 = -6 dB, 150 = +18 dB), `/bass <0-100>`, `/treble <0-100>`, `/voice`, `/relaystatus` |
| Effects | `.vol`, `.boost`, `.echo`, `.echolvl`, `.max`, `.reset` |
| Live voice | `.myboost`, `/livegain`, `.livevolume` |
| Automation | `.auto`, `.auto off`, `.ultra` |
| Library | `.audio`, `.audios`, `.myaudio`, `.saveaudio`; owner: `/addaudio` |
| Owner | `/owner`, `/users`, `/broadcast`, `/stats`, `/ban`, `/unban`, `/restart` |

Both `.` and `/` prefixes are supported where applicable.

## Audio notes

Playback recordings and downloaded audio are processed server-side by FFmpeg. The clarity-first default keeps echo disabled, removes rumble, lifts quiet speech, adds controlled presence, and normalizes the final loudness before limiting peaks. The `.echo on` option remains available, but echo can make speech less intelligible in a busy VC.

Telegram limits participant volume server-side. Therefore, the logged-in account’s live microphone can be set up to `20000` / `200%`, but other participants are not modified and server-side bass, echo, or compressor effects cannot be applied to a phone microphone. Those DSP effects are available for bot playback audio. The bot does not automatically detect or undo an external mute.

## Troubleshooting

| Problem | Check |
|---|---|
| `.play` does not start | Confirm that a VC is active, the logged-in account is a group member, the account is not muted by an administrator, and the source is valid. |
| Login fails | Recheck API credentials, OTP formatting, and 2FA password. Never share a String Session publicly. |
| Heroku bot is offline | Open **Resources** and enable the `worker` dyno; then inspect Heroku logs. |
| Audio is not loud enough | Use `.max`, or increase `/volume`, `/gain`, and `.boost` gradually. Keep `/bass` moderate for clarity. |
| Settings disappear after Heroku restart | Configure `MONGO_URI`; SQLite on an ephemeral dyno is not durable. |
| FFmpeg error | Confirm that the FFmpeg buildpack is present on Heroku or `ffmpeg -version` works locally. |

## License and security

Keep `.env`, bot tokens, API credentials, MongoDB URIs, and String Sessions private. Rotate any credential immediately if it is exposed in a chat, issue, log, or public repository.

Made with Pyrofork, PyTgCalls, FFmpeg, and yt-dlp.
