<div align="center">

# Apex VC Fight Bot

### Multi-user Telegram Voice Chat audio bot

<img src="assets/apex-vc-fight-bot.png" alt="Apex VC Fight Bot" width="420">

[![Owner](https://img.shields.io/badge/Owner-@TheY__CaIl__mE__OG-5865F2?logo=telegram&logoColor=white)](https://t.me/TheY_CaIl_mE_OG)
[![Updates](https://img.shields.io/badge/Updates-Apex%20Association-229ED9?logo=telegram&logoColor=white)](https://t.me/ApexAssociation)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

**[Owner](https://t.me/TheY_CaIl_mE_OG) · [Update Channel](https://t.me/ApexAssociation)**

</div>

Apex VC Fight Bot combines an in-bot Telegram login flow with button-driven controls, multi-user voice-chat sessions, FFmpeg audio processing, queue management, reusable tags, live participant volume control, and operational logging.

> **Important:** Telegram voice chats require a user account session. The bot account handles commands, while the logged-in user account joins the voice chat and plays audio.

## Highlights

| Capability | Description |
|---|---|
| In-bot authentication | Phone, OTP, optional 2FA, or a Pyrogram String Session. |
| Multi-user sessions | Each configured user receives an isolated Pyrogram and PyTgCalls engine. |
| Audio playback | FFmpeg processing with normalization, EQ, compression, gain, echo, and peak limiting. |
| Playback controls | Play, queue, force-play, loop, pause, resume, skip, stop, and queue inspection. |
| Audio library | Save audio messages, create reusable tags, and share owner-managed audio. |
| Live voice controls | Re-apply the logged-in account's participant volume after joins and reconnects. |
| Button-first interface | Home, login, settings, tutorial, playback, library, and owner controls. |
| Optional relay | Android Chrome microphone relay for supported VPS or Heroku deployments. |
| Persistent storage | MongoDB support for durable deployment, with SQLite available locally. |

## Deploy to Heroku

<a href="https://heroku.com/deploy?template=https://github.com/oyevipthoma88/vcfyt_bot"><img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy to Heroku"></a>

Use the deployment button, choose an application name, add the required Telegram values, and deploy. After deployment, open **Resources** and enable the `worker` dyno. The included `app.json` configures the Python and FFmpeg buildpacks.

For persistent Heroku storage, configure `MONGO_URI` before first use. The local SQLite database is suitable for development but is not durable across ephemeral dyno restarts.

## Local or VPS installation

### Requirements

Install Python, FFmpeg, and the project dependencies on the host.

```bash
sudo apt update
sudo apt install -y ffmpeg

git clone https://github.com/oyevipthoma88/vcfyt_bot.git
cd vcfyt_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Telegram credentials and deployment settings, then start the bot:

```bash
python main.py
```

## Telegram setup

Create an application at [my.telegram.org](https://my.telegram.org), create a bot with [@BotFather](https://t.me/BotFather), and obtain the numeric owner ID using [@userinfobot](https://t.me/userinfobot). Add the bot as an administrator in the log channel if operational logging is required.

The logged-in user account must be a member of every group where it will join a voice chat. The account should also have the permissions required to participate in the target voice chat.

## Configuration

| Variable | Required | Default | Purpose |
|---|---:|---:|---|
| `API_ID` / `API_HASH` | Yes | — | Telegram application credentials. |
| `BOT_TOKEN` | Yes | — | Token created through BotFather. |
| `OWNER_ID` or `OWNER_IDS` | Yes | — | Primary and additional numeric owner IDs. |
| `LOG_CHANNEL` | No | — | Channel or group for operational logs. |
| `STRING_SESSION` | No | — | Optional owner session; users can also log in through the bot. |
| `MONGO_URI` | Recommended on Heroku | SQLite | Persistent MongoDB connection URI. |
| `DEFAULT_VOLUME` | No | `1000` | Default playback volume from `0` to `1000`. |
| `DEFAULT_BOOST` | No | `10` | Loudness boost stage from `0` to `10`. |
| `DEFAULT_ECHO` | No | `false` | Enable echo by default. Keep disabled for clearer speech. |
| `DEFAULT_ECHO_LEVEL` | No | `2` | Echo intensity from `0` to `10`. |
| `RELAY_DEFAULT_VOLUME` | No | `1000` | Relay playback volume from `0` to `1000`. |
| `RELAY_DEFAULT_GAIN` | No | `150` | Relay gain from `0` to `150`. |
| `RELAY_DEFAULT_BASS` | No | `8` | Relay bass control from `0` to `100`. |
| `RELAY_DEFAULT_TREBLE` | No | `75` | Relay presence control from `0` to `100`. |
| `LIVE_BOOST_DEFAULT` | No | `20000` | Logged-in participant volume. |
| `AUTO_LIVE_BOOST` | No | `true` | Re-apply participant volume after joins and reconnects. |
| `AUDIO_ARCHIVE_CHANNEL` | No | — | Optional shared-audio archive channel. |
| `AUDIO_ARCHIVE_BOT_TOKEN` | No | — | Optional bot token used by the archive worker. |
| `MIC_RELAY_ENABLED` | No | `true` | Enable the browser microphone relay. |
| `MIC_RELAY_TOKEN` | Recommended for relay | — | Private token required by the microphone relay. |

For multiple owners, set `OWNER_IDS` as a comma- or semicolon-separated list. `OWNER_ID` remains supported as the primary owner variable.

## First-use flow

1. Send `/start` to the bot and open **Tutorial**.
2. Open **Login** and complete the phone, OTP, and optional 2FA flow, or provide a String Session.
3. Add the logged-in account to the target group and start a Telegram Voice Chat.
4. Reply to an audio or video message with `.play`, or play a saved tag.
5. Use `.padd` to queue, `.playforce` to replace the current track, `.loop` to repeat, and `.pause`, `.resume`, `.skip`, `.queue`, or `.stop` for transport control.
6. Use **Audio Library** to save audio, create tags, and share owner-managed audio.
7. Use `.myboost 20000` to set the logged-in account's live participant volume.

## Command reference

| Category | Commands |
|---|---|
| Account | `/start`, `/login`, `/addstring`, `/logout`, `/mystatus`, `/settings`, `/help` |
| Playback | `.play`, `.padd`, `.playforce`, `.fplay`, `.loop`, `.pause`, `.resume`, `.skip`, `.stop`, `.end`, `.leave`, `.queue`, `.vcinfo` |
| Tags | `.tag`, `.untag`, `.tags` |
| Audio | `/volume`, `/gain`, `/bass`, `/treble`, `/voice`, `/relaystatus` |
| Effects | `.vol`, `.boost`, `.echo`, `.echolvl`, `.max`, `.reset` |
| Live voice | `.myboost`, `/livegain`, `.livevolume` |
| Automation | `.auto`, `.auto off`, `.ultra` |
| Library | `.audio`, `.audios`, `.myaudio`, `.saveaudio`; owner: `/addaudio` |
| Owner | `/owner`, `/users`, `/broadcast`, `/stats`, `/ban`, `/unban`, `/restart` |

Both `.` and `/` prefixes are supported where applicable.

## Audio behavior

Audio and video files are processed server-side with FFmpeg before playback. The default chain removes rumble, lifts quiet audio, adds controlled presence, applies compression and gain, normalizes loudness, and limits peaks. Echo remains optional because it can reduce speech clarity in a busy voice chat.

Telegram controls participant volume server-side. The logged-in account can be set up to Telegram's supported participant-volume maximum, but other participants are not modified. Bass, echo, and compressor effects apply to bot playback audio rather than a phone microphone.

## Troubleshooting

| Problem | Check |
|---|---|
| `.play` does not start | Confirm that a voice chat is active, the logged-in account is a group member, the account is not muted, and the source is valid. |
| Login fails | Recheck API credentials, OTP formatting, and the optional 2FA password. Never share a String Session. |
| Heroku bot is offline | Open **Resources**, enable the `worker` dyno, and inspect the deployment logs. |
| Audio is not loud enough | Increase `/volume`, `/gain`, or `.boost` gradually, or use `.max`. Keep bass moderate for clarity. |
| Settings disappear after restart | Configure `MONGO_URI`; local SQLite is not durable on ephemeral Heroku storage. |
| FFmpeg errors occur | Verify `ffmpeg -version` locally or confirm that the Heroku FFmpeg buildpack is enabled. |

## Security

Keep `.env`, bot tokens, API credentials, MongoDB URIs, microphone relay tokens, and String Sessions private. Do not paste 2FA passwords or session strings into logs, issues, screenshots, or public repositories. Rotate credentials immediately if they are exposed.

## Project links

| Resource | Link |
|---|---|
| Owner | [@TheY_CaIl_mE_OG](https://t.me/TheY_CaIl_mE_OG) |
| Updates | [Apex Association](https://t.me/ApexAssociation) |
| Source code | [GitHub repository](https://github.com/oyevipthoma88/vcfyt_bot) |

## License

This repository does not currently include a license file. Add a license before redistributing the project commercially.

Built with [Pyrofork](https://github.com/Mayuri-Chan/pyrofork), [PyTgCalls](https://github.com/pytgcalls/pytgcalls), and [FFmpeg](https://ffmpeg.org/).
