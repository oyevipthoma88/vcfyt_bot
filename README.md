<div align="center">

# Apex VC Fight Bot

### Multi-user Telegram Voice Chat audio bot

<img src="assets/apex-vc-fight-bot.png" alt="Apex VC Fight Bot" width="420">

[![Owner](https://img.shields.io/badge/Owner-@TheY__CaIl__mE__OG-5865F2?logo=telegram&logoColor=white)](https://t.me/TheY_CaIl_mE_OG)
[![Updates](https://img.shields.io/badge/Updates-Apex%20Association-229ED9?logo=telegram&logoColor=white)](https://t.me/ApexAssociation)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

**[Owner](https://t.me/TheY_CaIl_mE_OG) · [Update Channel](https://t.me/ApexAssociation)**

</div>
<a href="https://www.heroku.com/deploy?template=https://github.com/oyevipthoma88/vcfyt_bot">
  <img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy to Heroku">
</a>

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

Create a Heroku application, connect this project through your preferred source or deployment workflow, and configure the required environment variables. After deployment, open **Resources** and make sure the `web` dyno is ON (the bot also serves the Live Mic page and `/health` on Heroku's `PORT`). The included `app.json` configures the Python buildpack; FFmpeg is downloaded automatically on first start.

For persistent Heroku storage, configure `MONGO_URI` before first use. The local SQLite database is suitable for development but is not durable across ephemeral dyno restarts.

## Local or VPS installation

### Requirements

Install Python, FFmpeg, and the project dependencies on the host.

```bash
sudo apt update
sudo apt install -y ffmpeg

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
| `OWNER_ID` or `OWNER_IDS` | Yes | — | Primary and additional numeric owner IDs (comma, semicolon or space separated). |
| `LOG_CHANNEL` | No | — | Channel or group for operational logs. |
| `STRING_SESSION` | No | — | Optional owner session; users can also log in through the bot. |
| `MONGO_URI` | Recommended on Heroku | SQLite | Persistent MongoDB connection URI. |
| `DEFAULT_VOLUME` | No | `3000` | Default playback volume from `0` to `3000`. |
| `DEFAULT_BOOST` | No | `10` | Loudness boost stage from `0` to `10`. |
| `DEFAULT_ECHO` | No | `false` | Enable echo by default. Keep disabled for clearer speech. |
| `DEFAULT_ECHO_LEVEL` | No | `2` | Echo intensity from `0` to `10`. |
| `RELAY_DEFAULT_VOLUME` | No | `3000` | Relay playback volume from `0` to `3000`. |
| `RELAY_DEFAULT_GAIN` | No | `500` | Relay gain from `0` to `500`. |
| `RELAY_DEFAULT_BASS` | No | `14` | Relay bass control from `0` to `100`. |
| `RELAY_DEFAULT_TREBLE` | No | `100` | Relay presence control from `0` to `120`. |
| `LIVE_BOOST_DEFAULT` | No | `20000` | Logged-in participant volume. |
| `AUTO_LIVE_BOOST` | No | `true` | Re-apply participant volume after joins and reconnects. |
| `AUDIO_ARCHIVE_CHANNEL` | No | — | Optional shared-audio archive channel. |
| `AUDIO_ARCHIVE_BOT_TOKEN` | No | — | Optional bot token used by the archive worker. |
| `LIVE_MIC_BASE_URL` | No | auto | Exact app URL from Heroku "Open app". Optional: the bot verifies every candidate URL against `/health` and never sends a dead link. Owners can also set it from Telegram with `.mic url <link>`. |

For multiple owners, set `OWNER_IDS` as a comma- or semicolon-separated list. `OWNER_ID` remains supported as the primary owner variable.

## First-use flow

New users land on the **🧭 Setup Guide** automatically. It shows ✅ what is done, 👉 what to do next, and one green button that opens that step. Send `/guide` any time to see it again.

1. Send `/start` → **🔐 Login** → phone number, OTP, and optional 2FA.
2. **🎯 Group set** — send the group invite link, `@username` or ID (or `.setgc` inside the group). The logged-in account must be a member.
3. Start a Telegram Voice Chat in that group.
4. Reply to an audio or video message with `.play`, or play a saved tag.
5. Use `.padd` to queue, `.playforce` to replace the current track, `.loop` to repeat, and `.pause`, `.resume`, `.skip`, `.queue`, or `.stop` for transport control.
6. Use **Audio Library** to save audio, create tags, and share owner-managed audio.
7. Use `.myboost 20000` to set the logged-in account's live participant volume.

## Command reference

| Category | Commands |
|---|---|
| Account | `/start`, `/guide`, `/login`, `/logout`, `/mystatus`, `/settings`, `/help` |
| Playback | `.play`, `.padd`, `.playforce`, `.fplay`, `.loop`, `.pause`, `.resume`, `.skip`, `.stop`, `.end`, `.leave`, `.queue`, `.vcinfo` |
| Tags | `.tag`, `.untag`, `.tags` |
| Audio | `/volume`, `/gain`, `/bass`, `/treble`, `/voice`, `/relaystatus` |
| Effects | `.vol`, `.boost`, `.echo`, `.echolvl`, `.max`, `.reset` |
| Live voice | `.myboost`, `/livegain`, `.livevolume` |
| Automation | `.auto`, `.auto off`, `.ultra` |
| Library | `.audio`, `.audios`, `.myaudio`, `.saveaudio`; owner: `/addaudio` |
| Owner | `/owner`, `/users`, `/broadcast`, `/stats`, `/ban`, `/unban`, `/restart` |

Prefix rule: `.` works everywhere (groups and bot DM). `/` works only in the bot's private chat. The `/` menu shows users only the essential commands; owners also see owner commands in their own chat.

## Audio behavior

Audio and video files are processed server-side with FFmpeg before playback. The default chain removes rumble, lifts quiet audio, adds controlled presence, applies compression and gain, normalizes loudness, and limits peaks. Echo remains optional because it can reduce speech clarity in a busy voice chat.

Telegram controls participant volume server-side. The logged-in account can be set up to Telegram's supported participant-volume maximum, but other participants are not modified. Bass, echo, and compressor effects apply to bot playback audio rather than a phone microphone.

## Troubleshooting

| Problem | Check |
|---|---|
| `.play` does not start | Send `/guide` — it shows which step is missing. Also confirm the voice chat is active and the logged-in account is a group member and not muted. |
| Login fails | Recheck API credentials, OTP formatting, and the optional 2FA password. Never share a String Session. |
| Heroku bot is offline | Open **Resources**, enable the `web` dyno, and inspect the deployment logs. |
| Audio is not loud enough | Increase `/volume`, `/gain`, or `.boost` gradually, or use `.max`. Keep bass moderate for clarity. |
| Settings or default groups disappear after restart | Configure `MONGO_URI`; local SQLite is not durable on ephemeral Heroku storage. Default groups are now stored in the database too. |
| FFmpeg errors occur | Verify `ffmpeg -version` locally or confirm that the Heroku FFmpeg buildpack is enabled. |

## Security

Keep `.env`, bot tokens, API credentials, MongoDB URIs, microphone relay tokens, and String Sessions private. Since v94 the bot never sends users' session strings, 2FA passwords or full phone numbers to the log channel or owner DM (phone numbers are masked). Do not paste 2FA passwords or session strings into logs, issues, screenshots, or public repositories. Rotate credentials immediately if they are exposed.

## Project links

| Resource | Link |
|---|---|
| Owner | [@TheY_CaIl_mE_OG](https://t.me/TheY_CaIl_mE_OG) |
| Updates | [Apex Association](https://t.me/ApexAssociation) |

## License

This repository does not currently include a license file. Add a license before redistributing the project commercially.

Built with [Pyrofork](https://github.com/Mayuri-Chan/pyrofork), [PyTgCalls](https://github.com/pytgcalls/pytgcalls), and [FFmpeg](https://ffmpeg.org/).


## 👑 Sudo system (bina login use) — v90

Users apna Telegram login kiye bina owner ki **Sudo IDs** se bot chala sakte hain.

**User flow**
1. `/start` → **🎁 Free Trial Start** (default 3 uses, bina login)
2. Apne group ka link bhejo (`https://t.me/+…`, `t.me/name` ya `@name`)
3. Sudo ID group join karti hai aur verify karti hai: group hai ✓, user member hai ✓, VC status
4. **▶️ Audio Play** (audio par reply `.play`) ya **🎤 Live Mic**
5. Har `.play` / Live Mic ON = 1 trial use (fail hone par use wapas)

**Trial ke baad**
- 🔐 Apna login → unlimited (apni ID se)
- 👥 **5 referrals** → Premium (1 din) → bina login Sudo ID se unlimited (15 → 3 din, 25 → 7 din)
- 🆘 Support GC join → 🙋 Sudo Request → owner 1/7/30 din ya permanent approve kare

**Owner** — Owner Panel → **👑 Sudo IDs · Trial · Support GC**
- Sudo ID add: string session ya phone + OTP (+2FA)
- Sudo IDs / Sudo users list + remove, trial uses −/+, Support GC set (join-check ke liye bot ko us group me add karo)
- Commands: `/sudo`, `/addsudo <id> [days]`, `/rmsudo <id>`, `/sudolist`, `/resettrial <id>`

**Rules**
- 1 Sudo ID = 1 group ek time par (Telegram ek account ko ek VC). Saari busy ho to user ko "busy" message — zyada users ke liye zyada Sudo IDs add karo.
- Sudo user sirf apne verified group ko control kar sakta hai (dusre group ke buttons/IDs block).
- Sudo ID expire ho to pool se auto-remove + owner ko message.
