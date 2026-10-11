
from pyrogram import Client, filters
from plugins.ui import HAS_USER, B
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from helpers.logger_channel import log_command
from plugins.ui import LINE, back_kb, edit_screen, safe_answer, source_button

TUTORIAL_MENU_TEXT = (
    "📘 <b>Apex vc fyt bot — Complete Tutorial</b>\n\n"
    "<blockquote>✨ Naye ho? Pehle <b>Quick Start</b> follow karein.\n"
    "🎧 Play & Queue se audio control karein.\n"
    "🎚️ Audio Settings se live controls badlein.\n"
    "🗃️ Played audio archive se duplicate download avoid hota hai.</blockquote>\n\n"
    "Har button mein commands, examples aur important tips diye gaye hain.\n\n"
    "ℹ️ Kisi bhi command ki poori info: <code>.help mic</code>, <code>.help ss</code>, "
    "<code>.help chat</code> — ya <code>.mic help</code>, <code>.play ?</code>"
)

def tutorial_kb() -> K:
    rows = [
        [B("🚀 Quick Start", callback_data="tut:quick"),
         B("🔐 Setup / Login", callback_data="tut:setup")],
        [B("▶️ Play & Queue", callback_data="tut:play"),
         B("🎚️ Audio Settings", callback_data="tut:effects")],
        [B("🗃️ Audio Library", callback_data="tut:library")],
        [B("🏷️ Tags", callback_data="tut:tags"),
         B("🔊 Live Voice Boost", callback_data="tut:boost")],
        [B("🎤 Live Mic", callback_data="tut:livemic")],
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
        "1️⃣ <code>/start</code> → <b>🔐 Login</b> → phone, OTP aur 2FA complete karein.\n"
        "2️⃣ Logged-in account ko target group mein add karein.\n"
        "3️⃣ Group mein Voice Chat start karein.\n"
        "4️⃣ Kisi bhi group/private chat mein audio/video ko reply karke <code>.play</code> bhejein; doosre group ke liye chat ID dein.\n"
        "5️⃣ Loudness ke liye <code>.max</code>; normal control ke liye "
        "<code>/volume</code> aur <code>/gain</code> (Audio Settings dekhein).\n"
        "6️⃣ Live mic (VC Bridge) ke liye <code>.mic on</code>; awaaz badhani ho to <code>.mic loud</code> — Live Mic tutorial dekhein.\n"
        "7️⃣ Repeat chahiye to track chalne ke baad <code>/loop</code>.\n"
        f"{LINE}\n\n"
        "🔊 Playback maximum practical loudness chain se process hota hai.\n"
        "💡 Speech clarity ke liye echo off rakhein; music ke liye hi echo on karein."
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
        "<code>.playmute</code> — audio reply karke unmute announcement set "
        "(<code>.playmute 3</code> ya <code>.playmute loop</code>)\n"
        "<code>.handraise on|off|now</code> — mute hote hi hand raise\n"
        "<code>.micblink on|off|5</code> — unmute ke baad pehle sirf mic on/off\n"
        "<code>.loud 0-18</code> — extra loudness\n"
        "<code>.ss on|off</code> — fake PC mic setup screen share\n"
        f"{LINE}\n\n"
        "▶ <b>Dot aur slash dono chalte hain</b> — <code>.play</code> ya "
        "<code>/play</code>, jo aasan lage."
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
        "<code>/volume &lt;0-1000&gt;</code> — playback volume (default 1000)\n"
        "<code>/gain &lt;0-400&gt;</code> — loudness gain (default 300)\n"
        "<code>/bass &lt;0-100&gt;</code> — controlled bass (default 10)\n"
        "<code>/treble &lt;0-120&gt;</code> — voice clarity/presence (default 100)\n"
        "<code>/voice female|male|normal</code> — voice profile\n"
        "<code>/relaystatus</code> — current relay settings\n"
        "<code>.vol &lt;0-1000&gt;</code> — playback volume control\n"
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
        "Owner audio ko reply karke <code>/addaudio Welcome</code> bhejega.\n"
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
        "<code>.myboost [1-20000]</code> — aapke logged-in account ki apni VC mic volume\n"
        "<code>/livegain [1-20000]</code> — wahi control\n"
        "VC join/reconnect par saved value apne aap dobara lagti hai.\n"
        f"{LINE}\n\n"
        "🎤 <b>Live Mic ki awaaz — 2 tarike, dono saath lagao:</b>\n\n"
        "<b>1️⃣ 🚀 200% boost (asli, bina phate)</b>\n"
        "Spare ID target group me sirf <b>Manage video chats</b> admin ho to uska 200% volume "
        "<b>har listener</b> ko milta hai = +6 dB. Bot ye apne aap karta hai (main ID ya bot admin ho to). "
        "Check: <code>.mic status</code> • Fix: <code>.mic boost</code>\n\n"
        "<b>2️⃣ 🔥 Drive (0–30)</b> — <code>.mic loud</code>\n"
        "• 0–8 saaf • 9–15 bohot tez • 16–20 FIGHT • 21–30 ULTRA (sabse tez, phategi)\n"
        "Ek tap: <b>💥 MAX</b> 20 • <b>⚔️ FIGHT</b> 26 • <b>☢️ ULTRA</b> 30\n\n"
        "Detail ke liye <b>🎤 Live Mic</b> tutorial dekhein."
    ),
    "livemic": (
        "🎤 <b>Live Mic — apni aawaz kisi bhi VC me, LOUD + clear</b>\n\n"
        f"{LINE}\n"
        "Aap <b>Mic Room</b> (private group) ki VC me bolte ho → spare ID sunti hai → "
        "server loudness chain lagata hai → spare ID <b>target VC</b> me wahi aawaz tez bolti hai.\n\n"
        f"{LINE}\n"
        "<b>⚡ Setup — sirf 3 kaam (pehli baar)</b>\n"
        "1. Bot me login: Home → 🔐 Login → Phone\n"
        "2. Bot DM: <code>.mic setup</code> → bot khud Mic Room banata hai, spare ID add karta hai, VC chalu karta hai\n"
        "3. Bot DM: <code>.mic chat -100TARGET</code> → jis group me aawaz jaani hai\n\n"
        "🙌 <b>Doosra login?</b> Owner ne spare pool lagaya hai to nahi chahiye — spare apne aap milti hai. "
        "Nahi to <code>.micaccount</code> → 📱 Phone se login (sirf number + OTP).\n\n"
        f"{LINE}\n"
        "<b>▶ Roz ka use</b>\n"
        "1. <code>.mic on</code> → <b>📲 Mic Room kholo</b> button dabao\n"
        "2. Room ki VC join karo, mic ON, 30 sec me bolna shuru\n"
        "3. Panel apne aap khulta hai — wahi se ON / OFF / Volume\n"
        "4. Band: <code>.mic off</code> • spare ko bahar: <code>.mic leave</code>\n\n"
        f"{LINE}\n"
        "<b>🔥 Awaaz max kaise</b>\n"
        "• <code>.mic status</code> me <b>🚀 200% boost: SABKE LIYE ✅</b> dikhe — nahi to <code>.mic boost</code>\n"
        "• <code>.mic loud fight</code> (26) ya <code>ultra</code> (30) • zyada phate → Drive kam / Clip SOFT\n"
        "• Bass 0 aur preset clean rakho — shabd sabse tez aate hain\n"
        "• Phone mic ke paas (10–15 cm) bolo, headphone lagao\n\n"
        f"{LINE}\n"
        "<b>🎚️ Presets</b>: <code>.mic on</code> clean • <code>bass</code> • <code>echo</code> • <code>full</code> • "
        "dusra target: <code>.mic on -100xxx</code>\n"
        "<b>🪑 Spare</b>: <code>.spare join</code> • <code>mute</code> / <code>unmute</code> • <code>leave</code>\n\n"
        f"{LINE}\n"
        "<b>🩺 Problem → Fix</b>\n"
        "• \"30 sec tak aawaz nahi\" → Mic Room VC me main ID ka mic ON hai? phir <code>.mic on</code>\n"
        "• Spare ID nahi mili → <code>.micaccount</code> ya owner se pool\n"
        "• Target me koi nahi sun raha → spare mute to nahi? <code>.spare unmute</code>\n"
        "• Main ID target VC me join na kare — ek account ek hi VC\n"
        "• Kuch bhi atka → <code>.mic leave</code> phir <code>.mic on</code>"
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
        "<code>.mic loud</code> — Live Mic (Bridge) ki awaaz badhayein"
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
        "🚪 <code>/logout</code> se apna session hata sakte hain."
    ),
    "faq": (
        "❓ <b>FAQ / Fixes</b>\n\n"
        f"{LINE}\n"
        "<b>Q. .play kaam nahi kar raha?</b>\n"
        "A. Group mein VC on hai? Aapka logged-in account us group ka "
        "member hai? chat ID negative hai?\n\n"
        "<b>Q. Aavaj kam lagti hai?</b>\n"
        "A. Playback: <code>.max</code> ya Audio Controls → MAX. Live mic: <code>.mic loud</code> → 🚀 Drive badhao "
        "ya <code>.mic loud fight</code>. Echo off rakhein, bass 0 rakhein.\n\n"
        "<b>Q. Kisi ki aavaj mute kaise karun?</b>\n"
        "A. Bot ab kisi ki aavaj kam/mute nahi karta — by design.\n"
        "\n<b>Q. Source Code button ka URL kaise badlein?</b>\n"
        "A. Owner DM mein <code>/setsource https://...</code> ya <code>/clearsource</code> use kare.\n"
        f"{LINE}"
    ),
    "cmds": (
        "📜 <b>All Commands</b>\n\n"
        "Har command <code>.</code> ya <code>/</code> dono se chalti hai.\n"
        "Detail: <code>.help &lt;command&gt;</code> ya <code>.&lt;command&gt; help</code>\n\n"
        f"{LINE}\n<b>Account</b>\n{LINE}\n"
        "/start  /login  /logout  /mystatus  /settings  /help\n\n"
        f"{LINE}\n<b>Playback</b>\n{LINE}\n"
        "/play  /padd  /playforce (/fplay)  /loop  /loop 5  /loop off\n"
        "/pause  /resume  /skip  /stop  /queue  /vcinfo  /auto\n"
        "/playmute  /handraise  /micblink  /loud  /ss\n\n"
        f"{LINE}\n<b>Tags</b>\n{LINE}\n"
        "/tag  /untag  /tags\n\n"
        f"{LINE}\n<b>Effects</b>\n{LINE}\n"
        "/volume  /vol  /gain  /bass  /treble  /voice  /boost\n"
        "/echo  /echolvl  /max  /reset  /relaystatus\n\n"
        f"{LINE}\n<b>Live</b>\n{LINE}\n"
        "/mic setup  /mic on  /mic off  /mic leave  /mic status\n"
        "/mic chat  /mic loud  /mic boost  /mic src  /myboost  /livegain\n"
        "/micaccount  /spare join|mute|unmute|leave\n\n"
        f"{LINE}\n<b>Library</b>\n{LINE}\n"
        "/audio  /saveaudio  /addaudio (owner)\n\n"
        f"{LINE}\n<b>Premium</b>\n{LINE}\n"
        "/redeem &lt;code&gt;  /referral  /mystatus\n\n"
        f"{LINE}\n<b>Owner</b>\n{LINE}\n"
        "/owner  /users  /stats  /broadcast  /restart  /ban  /unban\n"
        "/setsource  /clearsource  /setlog  /logtest\n"
        "/setprice  /grant  /revoke  /pending\n"
        "/addcoupon  /delcoupon  /listcoupons\n"
        "/setqr  /clearqr\n"
        "/addfsub  /delfsub  /listfsub  /clearfsub"
    ),
}

@Client.on_message(HAS_USER & filters.command("help", prefixes=[".", "/"]))
async def cmd_help(bot: Client, msg: Message):
    await log_command(msg.from_user.id if msg.from_user else 0,
                      msg.from_user.username if msg.from_user else "",
                      msg.chat.id, "/help")
    await msg.reply_text(TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())

@Client.on_callback_query(filters.regex(r"^tut:"))
async def cb_tutorial(bot, cq):
    key = cq.data.split(":", 1)[1]
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
