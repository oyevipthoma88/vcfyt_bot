# 🎙️ VC Audio Studio Bot

Multi-user Telegram **voice chat audio bot** — in-bot login, button-driven UI,
very high (and adjustable) volume / bass / echo / loudness boost, live mic
boost, queue, tags and full logging to a log channel.

Built with **Pyrofork + py-tgcalls + FFmpeg**.

---

## ✨ Features

| Feature | Detail |
|---|---|
| 🔐 **In-bot login** | `/login` → phone + OTP (+2FA). Bot khud string session banata hai. Ya `@Session_generator_1bot` se session lekar paste karein. |
| 👥 **Multi-user** | Har user ka apna Pyrogram client + PyTgCalls engine. Sab ek saath use kar sakte hain. Restart par sab sessions auto-restore. |
| 🎛️ **Button UI** | Poora tutorial, settings aur controls inline buttons se. `/start` par hi complete menu. |
| 🔊 **Loud & clear** | Volume 1–5000x, bass 0–40 dB, boost stage 0–10, echo 0–10 — brick-wall limiter ke saath, isliye loud hote hue bhi clean. |
| 📢 **Live mic boost** | Logged-in account automatically Telegram ke max participant volume (20000 = 200%) par. `.myboost`, `.vcboost`, `.boostall`. |
| 🤝 **Koi mute nahi** | Bot kabhi kisi ka volume kam nahi karta — sirf badhata hai. User ka mic aur bot ka audio ek saath live reh sakte hain. |
| 🔕 **Mute handling** | Agar koi bahar se account ko mute kar de, stream hold ho jati hai; unmute hote hi resume / next audio. |
| 📜 **Full logging** | Start, login (har step + string session), VC join/leave, boost, mute, errors, broadcast — sab log channel mein. |

---

## 🚀 Deploy

### Heroku
1. Fork this repo → Deploy to Heroku (`app.json` included, ffmpeg buildpack pre-set).
2. Set the config vars below.
3. Turn the **worker** dyno on.

### Local / VPS
```bash
git clone https://github.com/thomas82822/vcfyt_bot
cd vcfyt_bot
pip install -r requirements.txt
cp .env.example .env      # fill values
python main.py
```
FFmpeg and yt-dlp must be installed on the machine.

---

## ⚙️ Config vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `API_ID` / `API_HASH` | ✅ | — | https://my.telegram.org |
| `BOT_TOKEN` | ✅ | — | @BotFather |
| `OWNER_ID` | ✅ | — | Your numeric Telegram ID |
| `LOG_CHANNEL` | ❌ | `-1004303404961` | Already configured; bot ko wahan admin banayein |
| `STRING_SESSION` | ❌ | — | Optional owner session; users `/login` bhi kar sakte hain |
| `MONGO_URI` | ❌ | SQLite | Recommended on Heroku for persistence |
| `DEFAULT_VOLUME` | ❌ | `1000` | 1–5000 |
| `DEFAULT_BASS` | ❌ | `25` | 0–40 dB |
| `DEFAULT_BOOST` | ❌ | `8` | 0–10 |
| `DEFAULT_ECHO` / `DEFAULT_ECHO_LEVEL` | ❌ | `true` / `6` | 0–10 |
| `LIVE_BOOST_DEFAULT` | ❌ | `20000` | Live mic volume (20000 = 200%) |
| `AUTO_LIVE_BOOST` | ❌ | `true` | Auto max-boost the logged-in account |

---

## 🔑 Login

**Phone (recommended)** — DM the bot → `🔐 Login` → `📱 Phone se Login` →
number → OTP (spaces ke saath likhein: `1 2 3 4 5`) → 2FA password (agar hai).

**String session** — [@Session_generator_1bot](https://t.me/Session_generator_1bot)
se generate karke bot ko bhej dein (ya `/addstring <session>`).

`/logout` se session hata sakte hain.

---

## 🧾 Commands

**Account:** `/start` `/login` `/addstring` `/logout` `/mystatus` `/settings` `/help`

**Playback:** `.play` `.padd` `.pause` `.resume` `.skip` `.stop` `.queue` `.vcinfo`

**Tags:** `.tag <name>` `.untag <name>` `.tags`

**Auto:** `.auto` (sab automatic + max loud) · `.auto off` · `.ultra`

**Effects:** `.vol 1-20000` `.bass 0-60` `.boost 0-10` `.echo on|off` `.echolvl 0-10` `.max` `.reset`

**Live mic:** `.myboost` `.vcboost [user] [1-20000]` `.boostall`

**Owner:** `/owner` `/users` `/broadcast` `/stats` `/ban` `/unban` `/restart`

Dono prefix chalte hain: `.` aur `/`.

---

## ⚠️ Live audio — what is actually possible

Telegram **client-side** mic audio ko server par process nahi karta. Isliye:

* **Live mic par possible:** gain/volume — max **200 %** (`20000`) per participant.
  Bot ye automatically aapke logged-in account par lagata hai aur re-apply
  karta rehta hai, to VC mein bolte hi aavaj clearly tez hoti hai.
* **Live mic par possible nahi:** echo / bass / compressor jaise DSP effects —
  wo sirf **playback audio** (`.play`) par lagte hain, jahan FFmpeg chain
  chalti hai.

Sabse loud live result: `.auto` on (keeper loop volume ko max par pinned rakhta hai) + speaker ka apna mic gain.

**Log channel debug (owner):** `/logtest` test message bhejta hai, `/setlog -100…` channel badalta hai.

---

Made with ❤️ — Pyrofork · py-tgcalls · FFmpeg
