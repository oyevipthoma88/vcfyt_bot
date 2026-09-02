"""
Tutorial — fully button-driven. Har section ka apna button hai, koi
lamba scroll nahi.
"""

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from helpers.logger_channel import log_command
from plugins.ui import GEN_NAME, LINE, back_kb

TUTORIAL_MENU_TEXT = (
    "📖 <b>VC Fyt Bot — Complete Tutorial</b>\n\n"
    "Naye ho? Pehle <b>Quick Start</b> follow karein.\n"
    "Audio ko loud aur clear rakhne ke liye <b>Play & Queue</b> aur "
    "<b>Audio Settings</b> dekhein.\n\n"
    "Har button mein commands, examples aur important tips diye gaye hain."
)


def tutorial_kb() -> K:
    return K([
        [B("⚡ Quick Start", callback_data="tut:quick"),
         B("🚀 Setup / Login", callback_data="tut:setup")],
        [B("🎵 Play & Queue", callback_data="tut:play"),
         B("🎚️ Audio Settings", callback_data="tut:effects")],
        [B("🎧 Audio Library", callback_data="tut:library")],
        [B("🏷️ Tags", callback_data="tut:tags"),
         B("🔊 Live Voice Boost", callback_data="tut:boost")],
        [B("🎛️ VC Control", callback_data="tut:vc"),
         B("👥 Multi-User", callback_data="tut:multi")],
        [B("❓ FAQ / Fixes", callback_data="tut:faq"),
         B("📋 All Commands", callback_data="tut:cmds")],
        [B("🏠 Home", callback_data="menu:home")],
    ])


SECTIONS = {
    "quick": (
        "⚡ <b>Quick Start — 2 minute setup</b>\n\n"
        f"{LINE}\n"
        "1️⃣ `/start` ➜ <b>Login</b> ➜ phone/OTP/2FA complete karein.\n"
        "2️⃣ Logged-in account ko target group mein add karein.\n"
        "3️⃣ Group mein Voice Chat start karein.\n"
        "4️⃣ Kisi audio/video ko reply karke <code>.play</code> bhejein.\n"
        "5️⃣ Loudness ke liye <code>.max</code>; normal control ke liye "
        "<code>/volume 320</code> aur <code>/gain 60</code>.\n"
        "6️⃣ Live mic ke liye <code>.myboost 20000</code>.\n"
        f"{LINE}\n\n"
        "✅ Playback loud + clear default chain se process hota hai.\n"
        "💡 Speech clarity ke liye echo off rakhein; music ke liye hi echo on karein."
    ),
    "setup": (
        "🚀 <b>Setup — 4 steps</b>\n\n"
        f"{LINE}\n"
        "1️⃣ <b>Login</b> — Home ➜ 🔐 Login ➜ 📱 Phone se Login\n"
        "   (ya string session paste karein — generator: "
        f"{GEN_NAME})\n"
        "2️⃣ Jis group mein VC chalana hai, wahan <b>aapka logged-in "
        "account</b> member hona chahiye.\n"
        "3️⃣ Group mein <b>Voice Chat start</b> karein.\n"
        "4️⃣ Group mein <code>.play</code> (audio reply karke) — bas!\n"
        f"{LINE}\n\n"
        "💡 Bot ko group mein admin banane se logged-in account ka "
        "participant-volume control zyada reliably kaam karta hai.\n"
        "External admin mute ko bot automatically undo nahi karta.\n\n"
        "🔐 <b>Security:</b> OTP, 2FA password aur String Session kisi "
        "ke saath share na karein."
    ),
    "play": (
        "🎵 <b>Play & Queue</b>\n\n"
        f"{LINE}\n"
        "<code>.play</code> — audio/video message ko reply karke\n"
        "<code>.play &lt;tag&gt;</code> — saved tag chalayein\n"
        "<code>.play &lt;youtube/soundcloud url&gt;</code>\n"
        "<code>.play &lt;source&gt; &lt;chat_id&gt;</code> — PM se kisi group ke VC mein\n"
        "<code>.padd &lt;source&gt;</code> — queue mein add karein\n"
        "<code>.playforce</code> — sab hata kar turant chalao (alias <code>.fplay</code>)\n"
        "<code>.loop</code> / <code>.loop 5</code> / <code>.loop off</code> — repeat\n"

        f"{LINE}\n"
        "<code>.pause</code> / <code>.resume</code> / <code>.skip</code> / "
        "<code>.stop</code>\n"
        "<code>.queue</code> — queue dekhein\n"
        "<code>.vcinfo</code> — live status\n"
        f"{LINE}\n\n"
        "▶️ Slash bhi chalta hai: <code>/play</code>"
    ),
    "tags": (
        "🏷️ <b>Tags — apni favourite audio save karein</b>\n\n"
        f"{LINE}\n"
        "<code>.tag &lt;name&gt;</code> — audio/video ko reply karke save\n"
        "<code>.tags</code> — apni saari tags\n"
        "<code>.untag &lt;name&gt;</code> — delete\n"
        f"{LINE}\n\n"
        "Example:\n"
        "<code>.tag intro</code> ➜ baad mein <code>.play intro</code>"
    ),
    "effects": (
        "🎚️ <b>Audio Effects — high bhi, low bhi</b>\n\n"
        f"{LINE}\n"
        "<code>/volume &lt;0-1000&gt;</code> — playback volume (default 1000)\n"
        "<code>/gain &lt;0-150&gt;</code> — loudness gain (default 150)\n"
        "<code>/bass &lt;0-100&gt;</code> — controlled bass (default 8)\n"
        "<code>/treble &lt;0-100&gt;</code> — voice clarity/presence (default 62)\n"
        "<code>/voice female|male|normal</code> — voice profile\n"
        "<code>/relaystatus</code> — current relay settings\n"
        "<code>.vol &lt;0-1000&gt;</code> — playback volume control\n"
        "<code>.boost &lt;0-10&gt;</code> — loudness stage (default 9)\n"
        "<code>.echo on|off</code> — echo toggle\n"
        "<code>.echolvl &lt;0-10&gt;</code> — echo kitna heavy\n"
        "<code>.max</code> — sab kuch maximum 🔥\n"
        "<code>.reset</code> — default settings\n"
        f"{LINE}\n\n"
        "🎛️ Ya Home ➜ <b>Audio Settings</b> se buttons se badhaayein/ghataayein.\n"
        "Change turant chal rahe track par apply hota hai.\n\n"
        "🎙️ Voice ke liye: bass moderate, treble 55–75, gain 50–70.\n"
        "🎵 Music ke liye: bass 15–30 try karein; distortion aaye to gain kam karein."
    ),
    "library": (
        "🎧 <b>Audio Library — examples</b>\n\n"
        f"{LINE}\n"
        "<b>Apna audio save karein</b>\n"
        "1️⃣ Audio/video message ko reply karein.\n"
        "2️⃣ <code>.saveaudio My Intro</code> bhejein.\n"
        "3️⃣ <code>.audio</code> ➜ <b>My Audio</b> se list dekhein.\n\n"
        "<b>Owner ke shared audios</b>\n"
        "Owner audio ko reply karke <code>/addaudio Welcome</code> bhejega.\n"
        "Sab users <code>.audio</code> ➜ <b>Bot Audios</b> mein use dekh sakte hain.\n\n"
        "<b>Play</b>\n"
        "Pehle apna logged-in account VC mein join karein, phir audio ke saamne "
        "<b>Play in active VC</b> dabayein.\n"
        "My Audio sirf aap delete kar sakte hain; Bot Audios owner manage karta hai."
    ),
    "boost": (
        "🔊 <b>Live Voice Boost</b>\n\n"
        f"{LINE}\n"
        "<code>.myboost [1-20000]</code> — apne active logged-in account ki live mic gain\n"
        "<code>/livegain [1-20000]</code> — same live mic control\n"
        "VC join/reconnect par saved value automatically re-apply hoti hai.\n"
        f"{LINE}\n\n"
        "✅ Login karte hi aapka account <b>automatically</b> max live "
        "volume par set ho jata hai — VC mein bolte hi aavaj tez.\n\n"
        "⚠️ <b>Sach ye hai:</b> Telegram live mic par sirf <b>gain</b> "
        "(200% max) allow karta hai. Echo/bass jaise effects live mic par "
        "server-side possible nahi — wo playback audio par lagte hain. "
        "Isliye live ke liye max gain + auto re-apply use hota hai."
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
        "<code>.myboost</code> — apni live mic ko boost karein"
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
        "🔐 <code>/logout</code> se apna session hata sakte hain."
    ),
    "faq": (
        "❓ <b>FAQ</b>\n\n"
        f"{LINE}\n"
        "<b>Q. String session kahan se laun?</b>\n"
        f"A. {GEN_NAME} — ya bot mein hi 📱 Phone Login karein.\n\n"
        "<b>Q. .play kaam nahi kar raha?</b>\n"
        "A. Group mein VC on hai? Aapka logged-in account us group ka "
        "member hai? chat ID negative hai?\n\n"
        "<b>Q. Aavaj kam lagti hai?</b>\n"
        "A. <code>.max</code> ya Audio Settings ➜ 🔥 MAX. Echo off rakhein, "
        "aur `/gain 60` + `/treble 62` try karein.\n\n"
        "<b>Q. Kisi ki aavaj mute kaise karun?</b>\n"
        "A. Bot ab kisi ki aavaj kam/mute nahi karta — by design.\n"
        f"{LINE}"
    ),
    "cmds": (
        "📋 <b>All Commands</b>\n\n"
        f"{LINE}\n<b>Account</b>\n{LINE}\n"
        "/start /login /addstring /logout /mystatus /settings /help\n\n"
        f"{LINE}\n<b>Playback</b>\n{LINE}\n"
        ".play  .padd  .playforce  .loop  .pause  .resume  .skip  .stop  .queue  .vcinfo\n\n"
        f"{LINE}\n<b>Tags</b>\n{LINE}\n"
        ".tag  .untag  .tags\n\n"
        f"{LINE}\n<b>Effects</b>\n{LINE}\n"
        "/volume  /gain  /bass  /treble  /voice  /relaystatus\n"
        ".vol  .boost  .echo  .echolvl  .max  .reset\n\n"
        f"{LINE}\n<b>Live</b>\n{LINE}\n"
        ".myboost\n\n"
        f"<b>Library</b>\n{LINE}\n"
        ".audio  .saveaudio  /addaudio\n\n"
        f"<b>Owner</b>\n{LINE}\n"
        "/owner /users /broadcast /stats /restart /ban /unban"
    ),
}


@Client.on_message(filters.command("help"))
async def cmd_help(bot: Client, msg: Message):
    await log_command(msg.from_user.id if msg.from_user else 0,
                      msg.from_user.username if msg.from_user else "",
                      msg.chat.id, "/help")
    await msg.reply_text(TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())


@Client.on_callback_query(filters.regex(r"^tut:"))
async def cb_tutorial(bot, cq):
    key = cq.data.split(":", 1)[1]
    if key == "menu":
        await cq.message.edit_text(TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())
        await cq.answer()
        return
    text = SECTIONS.get(key)
    if not text:
        await cq.answer("Not found")
        return
    await cq.message.edit_text(text, reply_markup=back_kb("tut:menu"))
    await cq.answer()
