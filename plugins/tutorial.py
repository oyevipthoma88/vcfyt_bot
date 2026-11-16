
from pyrogram import Client, filters
from plugins.ui import dm_cmd
from plugins.ui import HAS_USER, B
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from helpers.logger_channel import log_command
from plugins.ui import LINE, back_kb, edit_screen, safe_answer, source_button

from plugins.ui import card as _card
TUTORIAL_MENU_TEXT = _card("📘", "Help & Tutorial",
    "🚀 <b>Naye ho?</b> → <b>Quick Start</b> (2 min)\n"
    "🎤 <b>Live Mic</b> → phone se bolo, VC me tez + saaf\n"
    "▶️ <b>Play & Queue</b> → audio chalao\n"
    "🎚️ <b>Audio Settings</b> → sound tuning\n"
    "❓ <b>FAQ / Fixes</b> → koi dikkat? yahan dekho",
    "Shortcut: <code>.help mic</code> · <code>.help ss</code> · <code>.play ?</code>")

def tutorial_kb() -> K:
    rows = [
        [B("🎬 Video Tutorial (1 min)", callback_data="tut:video", style="success")],
        [B("🚀 Quick Start", callback_data="tut:quick"),
         B("🔐 Setup / Login", callback_data="tut:setup")],
        [B("🎤 Live Mic — Web (tez + saaf)", callback_data="tut:livemic", style="success")],
        [B("▶️ Play & Queue", callback_data="tut:play"),
         B("🎚️ Audio Settings", callback_data="tut:effects")],
        [B("🗃️ Audio Library", callback_data="tut:library")],
        [B("🏷️ Tags", callback_data="tut:tags"),
         B("🔊 Live Voice Boost", callback_data="tut:boost")],
        [B("🎛️ VC Control", callback_data="tut:vc"),
         B("👥 Multi-User", callback_data="tut:multi")],
        [B("❓ FAQ / Fixes", callback_data="tut:faq"),
         B("📜 All Commands", callback_data="tut:cmds")],
        [B("🏠 Home", callback_data="menu:home")],
    ]
    rows.extend(source_button())
    return K(rows)

SECTIONS = {
    "quick": (
        "🚀 <b>Quick Start — 2 minute setup</b>\n\n"
        f"{LINE}\n"
        "1️⃣ <code>.start</code> → <b>🔐 Start Setup</b> → phone, OTP (aur 2FA) bhejo.\n"
        "2️⃣ Logged-in ID ko target group me add karo.\n"
        "3️⃣ Group me <code>.setgc</code> bhejo <i>(sirf ek baar)</i>.\n"
        "4️⃣ Group me Voice Chat start karo.\n"
        "5️⃣ Audio pe reply karke <code>.play</code> — ya <b>🎤 Live Mic</b> se khud bolo.\n"
        f"{LINE}\n\n"
        "⚡ Sabse tez playback: <code>.tez</code> / <code>.max</code>\n"
        "🎤 Live mic: <code>.mic on</code> → link Chrome me kholo → 🎙 dabao\n"
        "🔁 Repeat: <code>.loop</code>  ·  ⏹ Band: <code>.stop</code>"
    ),
    "setup": (
        "🔐 <b>Setup — 4 steps</b>\n\n"
        f"{LINE}\n"
        "1️⃣ <b>Login</b> — Home → 🔐 Login → Phone se Login\n"
        "2️⃣ Jis group mein VC chalana hai, wahan <b>aapka logged-in "
        "account</b> member hona chahiye.\n"
        "3️⃣ Group mein <b>Voice Chat start</b> karein.\n"
        "4️⃣ Group mein <code>.play</code> (audio reply karke) — bas!\n"
        f"{LINE}\n\n"
        "💡 Bot ko group mein admin banane se logged-in account ka "
        "participant-volume control zyada reliably kaam karta hai.\n"
        "External admin mute ko bot automatically undo nahi karta.\n\n"
        "🔒 <b>Security:</b> OTP aur 2FA password kisi ke saath share na karein."
    ),
    "play": (
        "▶️ <b>Play & Queue</b>\n\n"
        f"{LINE}\n"
        "<code>.play</code> — isi group/chat ke VC mein reply audio chalayein\n"
        "<code>.play &lt;chat_id&gt;</code> — reply audio ko target group VC mein chalayein\n"
        "<code>.play &lt;tag&gt;</code> — saved tag isi group mein chalayein\n"
        "Reply to a Telegram audio/video message or use a saved tag.\n"
        "<code>.play &lt;source&gt; &lt;chat_id&gt;</code> — kisi bhi chat se target group VC mein\n"
        "Example: <code>.play myaudio -1001234567890</code>\n"
        "Group ke andar target ID optional hai: <code>.play myaudio</code>.\n"
        "Reply audio ke liye shortcut: <code>.play -1001234567890</code>.\n"
        "<code>.padd &lt;source&gt;</code> — queue mein add karein\n"
        "<code>.playforce</code> — sab hata kar turant chalao (alias <code>.fplay</code>)\n"
        "<code>.loop</code> / <code>.loop 5</code> / <code>.loop off</code> — repeat\n"

        f"{LINE}\n"
        "<code>.pause</code> / <code>.resume</code> / <code>.skip</code> / "
        "<code>.stop</code>\n"
        "<code>.queue</code> — queue dekhein\n"
        "<code>.vcinfo</code> — live status\n"
        f"{LINE}\n"
        "<b>Admin mute / unmute (human mode)</b>\n"
        "Admin mute kare → 2-3 sec me ✋ hand raise. Unmute → mic ON (bina blink), "
        "10-15 sec baad PlayMute audio poora, phir main audio shuru se.\n"
        "<code>.playmute</code> — audio reply karke PlayMute audio set "
        "(<code>.playmute 3</code> ya <code>.playmute loop</code>, hatao: <code>.pmclr</code>)\n"
        "<code>.handraise on|off|now</code> — human mode on/off\n"
        "<code>.micblink on|off|5</code> — default OFF; ON = unmute ke baad mic blink\n"
        "<code>.loud 0-18</code> — extra loudness\n"
        "<code>.ss on|off</code> — fake PC mic setup screen share\n"
        f"{LINE}\n\n"
        "▶ <b>Dot aur slash dono chalte hain</b> — <code>.play</code> ya "
        "<code>.play</code>, jo aasan lage."
    ),
    "tags": (
        "🏷️ <b>Tags — apni favourite audio save karein</b>\n\n"
        f"{LINE}\n"
        "<code>.tag &lt;name&gt;</code> — audio/video ko reply karke save\n"
        "<code>.tags</code> — apni saari tags\n"
        "<code>.untag &lt;name&gt;</code> — delete\n"
        f"{LINE}\n\n"
        "Example:\n"
        "<code>.tag intro</code> → baad mein <code>.play intro</code>"
    ),
    "effects": (
        "🎚️ <b>Audio Settings — high bhi, low bhi</b>\n\n"
        f"{LINE}\n"
        "<code>.volume &lt;0-3000&gt;</code> — playback volume (default 3000)\n"
        "<code>.gain &lt;0-500&gt;</code> — loudness gain (default 300)\n"
        "<code>.bass &lt;0-100&gt;</code> — controlled bass (default 10)\n"
        "<code>.treble &lt;0-120&gt;</code> — voice clarity/presence (default 100)\n"
        "<code>.voice female|male|normal</code> — voice profile\n"
        "<code>.relaystatus</code> — current relay settings\n"
        "<code>.vol &lt;0-3000&gt;</code> — playback volume control\n"
        "<code>.boost &lt;0-10&gt;</code> — loudness stage (default 10)\n"
        "<code>.echo on|off</code> — echo toggle\n"
        "<code>.echolvl &lt;0-10&gt;</code> — echo kitna heavy\n"
        "<code>.max</code> — sab kuch maximum 🔥\n"
        "<code>.reset</code> — default settings\n"
        f"{LINE}\n\n"
        "👉 Ya Home → <b>🎚️ Audio Controls</b> se buttons se badhaayein/ghataayein.\n"
        "Change turant chal rahe track par apply hota hai.\n\n"
        "🗣️ Voice ke liye: bass kam, treble 80–100, gain 200–300.\n"
        "🎵 Music ke liye: bass 15–30 try karein; distortion aaye to gain kam karein."
    ),
    "library": (
        "🗃️ <b>Audio Library — examples</b>\n\n"
        f"{LINE}\n"
        "<b>Apna audio save karein</b>\n"
        "1️⃣ Audio/video message ko reply karein.\n"
        "2️⃣ <code>.saveaudio My Intro</code> bhejein.\n"
        "3️⃣ <code>.audio</code> → <b>My Audio</b> se list dekhein.\n\n"
        "<b>Owner ke shared audios</b>\n"
        "Owner audio ko reply karke <code>.addaudio Welcome</code> bhejega.\n"
        "Sab users <code>.audio</code> → <b>Bot Audios</b> mein use dekh sakte hain.\n\n"
        "<b>Play</b>\n"
        "Audio ke saamne <b>Send Audio</b> dabayein. Audio ko kisi bhi chat mein reply karke "
        "<code>.tag myaudio</code> likhein.\n"
        "Phir target group ke VC mein <code>.play myaudio &lt;chat_id&gt;</code> likhein.\n"
        "Example: <code>.play myaudio -1001234567890</code>. Group ke andar ho to "
        "sirf <code>.play myaudio</code> bhi chalega.\n"
        "My Audio sirf aap delete kar sakte hain; Bot Audios owner manage karta hai.\n\n"
        "📌 Jo audio VC mein play hota hai, wo configured archive channel mein ek baar save hota hai; repeat par duplicate skip hota hai."
    ),
    "boost": (
        "🔊 <b>Live Voice Boost</b>\n\n"
        f"{LINE}\n"
        "<code>.myboost [1-20000]</code> — aapke logged-in account ki VC mic volume\n"
        "<code>.livegain [1-20000]</code> — wahi control\n"
        "VC join/reconnect par saved value apne aap lagti hai.\n"
        f"{LINE}\n\n"
        "🎤 <b>Web Live Mic</b> ki awaaz:\n"
        "• Mic page par <b>MIC BOOST</b> slider (50–400%) — turant lagta hai\n"
        "• <code>.micmode loudest</code> (default, sabse tez) / <code>phone</code> (saaf) / <code>bass</code> (bhari)\n"
        "• Level <b>green</b> rakho = tez + saaf"
    ),
    "livemic": (
        "🎤 <b>Live Mic — phone se bolo, VC me TEZ + SAAF</b>\n\n"
        f"{LINE}\n"
        "Aap phone ke <b>Chrome</b> me ek private page kholte ho aur bolte ho. "
        "Bot ki ID wahi awaaz group VC me loud + clear bolti hai.\n\n"
        f"{LINE}\n"
        "<b>▶ Pehli baar (ek hi baar)</b>\n"
        "1. Home → <b>⚔️ Fight ID jodo</b> (number + OTP) — wo ID jo VC me bolegi\n"
        "   👤 Apni main ID dena <b>zaroori nahi</b>\n"
        "2. Fight ID ko apne group me add karo\n"
        "3. Live Mic → 🎯 Group set karo (invite link / @username / ID)\n"
        "4. Group me Voice Chat start karo\n\n"
        "<b>▶ Roz ka use</b>\n"
        "1. <code>.mic on</code> ya Home → 🎤 Live Mic → <b>MIC ON</b>\n"
        "2. <b>🎙 MIC KHOLO</b> button → <b>Chrome</b> me khulega\n"
        "3. Bada 🎙 button dabao → <b>Allow</b> → bolo\n"
        "4. Band: page par 🎙 dobara ya <code>.mic off</code>\n\n"
        f"{LINE}\n"
        "<b>🔊 Sabse tez + saaf awaaz</b>\n"
        "• 🎧 <b>Earphone lagao</b> (VC ki awaaz mic me wapas nahi aati)\n"
        "• Mic muh se 3–5 cm door\n"
        "• <b>MIC LEVEL</b> green rakho — red = <b>MIC BOOST</b> kam karo, dheemi = badhao\n"
        "• Mode: ⚡ <b>FIGHT</b> (tez) · ✨ <b>CLEAN</b> (agar phate)\n"
        "• Kisi ID ko group admin banao → 200% volume sabke liye\n"
        "• Page khula rakho, screen lock mat karo\n\n"
        f"{LINE}\n"
        "<b>🩺 Problem → Fix</b>\n"
        "• <b>Permission block</b> → address bar 🔒 → Microphone → Allow → reload\n"
        "• <b>Telegram ke andar mic nahi chala</b> → <b>Chrome me kholein</b> dabao\n"
        "• <b>“Mic se awaaz nahi aa rahi”</b> → Telegram app me apna VC mic MUTE / leave karo\n"
        "• <b>Koi sun nahi raha</b> → Fight ID group me mute to nahi? <code>.mic status</code>\n"
        "• <b>Echo aa raha</b> → page par 🔊 <b>Speaker</b> mode\n"
        "• <b>Link expire</b> → fresh <code>.mic on</code>"
    ),
    "vc": (
        "🎛️ <b>VC Control</b>\n\n"
        f"{LINE}\n"
        "• Bot other participants ki volume ya mute state ko touch nahi karta.\n"
        "• Live participant-volume control sirf logged-in account par apply hota hai.\n"
        "• Aapka mic aur bot ka audio <b>ek saath</b> chal sakte hain.\n"
        "• Playback controls sirf bot ke apne audio stream par apply hote hain.\n"
        f"{LINE}\n"
        "<code>.vcinfo</code> — status\n"
        "<code>.stop</code> — VC chhod dein\n"
        "<code>.myboost</code> — apni live mic ko boost karein\n"
        "<code>.micmode loudest|phone|bass</code> — Live Mic ki awaaz ka mode"
    ),
    "multi": (
        "👥 <b>Multi-User</b>\n\n"
        f"{LINE}\n"
        "• Har user apne <b>apne account</b> se login karta hai.\n"
        "• Sabke alag VC session — ek saath alag groups mein chalega.\n"
        "• Aapki commands <b>sirf aapke</b> account par asar karti hain.\n"
        "• Bot restart hone par sabhi logins <b>auto restore</b> ho jate "
        "hain.\n"
        f"{LINE}\n\n"
        "🚪 <code>.logout</code> se apna session hata sakte hain."
    ),
    "faq": (
        "❓ <b>FAQ / Fixes</b>\n\n"
        f"{LINE}\n"
        "<b>Q. .play kaam nahi kar raha?</b>\n"
        "A. Group mein VC on hai? Aapka logged-in account us group ka "
        "member hai? chat ID negative hai?\n\n"
        "<b>Q. Aavaj kam lagti hai?</b>\n"
        "A. Playback: <code>.tez</code> / <code>.max</code>. Live mic: page par <b>MIC BOOST</b> badhao, "
        "earphone lagao, mic 3–5 cm door, mode ⚡ FIGHT.\n\n"
        "<b>Q. Live mic ki awaaz phat rahi / khar-khar?</b>\n"
        "A. MIC LEVEL red hai to MIC BOOST kam karo, ya ✨ CLEAN mode.\n\n"
        "<b>Q. Kisi ki aavaj mute kaise karun?</b>\n"
        "A. Bot ab kisi ki aavaj kam/mute nahi karta — by design.\n"
        "\n<b>Q. Source Code button ka URL kaise badlein?</b>\n"
        "A. Owner DM mein <code>/setsource https://...</code> ya <code>/clearsource</code> use kare.\n"
        f"{LINE}"
    ),
    "cmds": (
        "📜 <b>ALL COMMANDS</b>\n"
        f"<code>{LINE}</code>\n"
        "<i>Har command <code>.</code> ya <code>/</code> se chalti hai · detail: <code>.help play</code></i>\n\n"
        "⚡ <b>Sabse tez awaaz</b>\n<blockquote><code>.tez</code> · <code>.loudmode</code> · <code>.micmode</code> · <code>.max</code></blockquote>\n"
        "▶️ <b>Playback</b>\n<blockquote><code>.play</code> <code>.padd</code> <code>.fplay</code> <code>.loop</code>\n"
        "<code>.pause</code> <code>.resume</code> <code>.skip</code> <code>.stop</code> <code>.queue</code>\n"
        "<code>.vcinfo</code> <code>.auto</code> <code>.playmute</code> <code>.ss</code>\n"
        "<code>.handraise</code> <code>.micblink</code></blockquote>\n"
        "🎚️ <b>Sound</b>\n<blockquote><code>.volume</code> <code>.gain</code> <code>.bass</code> <code>.treble</code>\n"
        "<code>.voice</code> <code>.boost</code> <code>.echo</code> <code>.echolvl</code> <code>.reset</code></blockquote>\n"
        "🎤 <b>Live mic</b>\n<blockquote><code>.mic on|off|leave|status</code>\n"
        "<code>.mic chat</code> <code>.micmode</code> <code>.livegain</code>\n"
        "<code>.micaccount</code> <code>.spare join|mute|unmute|leave</code></blockquote>\n"
        "🔗 <b>Multi-ID</b>\n<blockquote><code>.syncplay</code></blockquote>\n"
        "🏷️ <b>Tags & Library</b>\n<blockquote><code>.tag</code> <code>.untag</code> <code>.tags</code> <code>.audio</code> <code>.saveaudio</code></blockquote>\n"
        "👤 <b>Account & Premium</b>\n<blockquote><code>.start</code> <code>.login</code> <code>.logout</code> <code>.mystatus</code>\n"
        "<code>.redeem</code> <code>.referral</code></blockquote>\n"
        "👑 <b>Owner</b>\n<blockquote expandable>.owner .users .stats .broadcast .restart .ban .unban\n"
        ".setsource .clearsource .setlog .logtest .setprice .grant .revoke .pending\n"
        ".addcoupon .delcoupon .listcoupons .setqr .clearqr\n"
        ".addfsub .delfsub .listfsub .clearfsub</blockquote>"
    ),
}

import os

_VIDEO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets", "tutorial.mp4")
_video_file_id = None  # pehli upload ke baad file_id cache — dobara upload nahi

VIDEO_CAPTION = (
    "🎬 <b>Live Mic — 1 minute tutorial</b>\n\n"
    "1️⃣ 🔐 Login  2️⃣ 🎯 <code>.mic chat -100…</code>\n"
    "3️⃣ 🟢 <code>.mic on</code>  4️⃣ Chrome me 🎙 dabao — bas!\n\n"
    "👇 Shuru karne ke liye <b>🎤 Live Mic</b> dabao."
)


def _video_kb() -> K:
    return K([[B("🎤 Live Mic", callback_data="mic:panel", style="success")],
              [B("📘 Madad", callback_data="tut:menu", style="primary"),
               B("🏠 Home", callback_data="menu:home", style="primary")]])


TUTORIAL_VIDEO_KEY = "tutorial_video_file_id"


async def send_tutorial_video(message) -> bool:
    """Tutorial video bhejta hai (env TUTORIAL_VIDEO > cached file_id > assets/tutorial.mp4)."""
    from helpers.database import db
    try:
        src = await db.get_app_value(TUTORIAL_VIDEO_KEY)
    except Exception:
        src = None
    if not src:
        return False  # owner ne .setvideo se video set nahi kiya
    try:
        await message.reply_video(src, caption=VIDEO_CAPTION, reply_markup=_video_kb(),
                                  supports_streaming=True)
        return True
    except Exception:
        return False


@Client.on_message(HAS_USER & dm_cmd(["video", "guide", "tutorial"]) & filters.private)
async def cmd_video(bot: Client, msg: Message):
    if not await send_tutorial_video(msg):
        await msg.reply_text(TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())


@Client.on_message(HAS_USER & dm_cmd("help"))
async def cmd_help(bot: Client, msg: Message):
    await log_command(msg.from_user.id if msg.from_user else 0,
                      msg.from_user.username if msg.from_user else "",
                      msg.chat.id, ".help")
    await msg.reply_text(TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())

@Client.on_callback_query(filters.regex(r"^tut:"))
async def cb_tutorial(bot, cq):
    key = cq.data.split(":", 1)[1]
    if key == "video":
        await safe_answer(cq, "🎬 Video aa raha hai…")
        if not await send_tutorial_video(cq.message):
            await edit_screen(cq.message, SECTIONS["livemic"], reply_markup=back_kb("tut:menu"))
        return
    if key == "menu":
        await edit_screen(cq.message, TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())
        await safe_answer(cq)
        return
    text = SECTIONS.get(key)
    if not text:
        await safe_answer(cq, "Not found")
        return
    await edit_screen(cq.message, text, reply_markup=back_kb("tut:menu"))
    await safe_answer(cq)
