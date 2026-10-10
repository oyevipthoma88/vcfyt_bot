"""Per-command help.

* `.help <cmd>`  /  `/help <cmd>`  -> full info + how to use for that command
* `.<cmd> help`  /  `.<cmd> ?`     -> same, without running the command
* Unknown dot command in bot DM    -> "did you mean" suggestion
"""

import difflib
import re

from pyrogram import Client, filters
from pyrogram.types import Message

from plugins.ui import HAS_USER

# name: (aliases, usage lines, description, example)
HELP = {
    "play": ([], ".play  |  .play <tag>  |  .play <tag> <chat_id>  |  .play <chat_id>",
             "Audio/video ko reply karke VC me chalata hai. Group me chat ID ki zarurat nahi.",
             "Audio reply karke: .play\n.play intro -1001234567890"),
    "padd": ([], ".padd <tag>  (ya audio reply karke .padd)", "Track ko queue me add karta hai.", ".padd intro"),
    "playforce": (["fplay"], ".playforce <tag>", "Queue hata kar turant ye track chalata hai.", ".fplay intro"),
    "pause": ([], ".pause", "Chal raha audio rok deta hai.", ".pause"),
    "resume": ([], ".resume", "Ruka hua audio phir chalata hai.", ".resume"),
    "skip": ([], ".skip", "Agla track chalata hai.", ".skip"),
    "stop": (["end", "leave"], ".stop", "Playback band karke VC chhod deta hai.", ".stop"),
    "queue": ([], ".queue", "Queue ki list dikhata hai.", ".queue"),
    "qclear": ([], ".qclear", "Poori queue saaf.", ".qclear"),
    "qremove": ([], ".qremove <number>", "Queue se ek track hatata hai.", ".qremove 2"),
    "qshuffle": ([], ".qshuffle", "Queue shuffle karta hai.", ".qshuffle"),
    "loop": ([], ".loop  |  .loop 5  |  .loop off", "Track repeat karta hai (hamesha / N baar / band).", ".loop 3"),
    "auto": ([], ".auto on|off", "Auto-play mode on/off.", ".auto on"),
    "vcinfo": ([], ".vcinfo", "VC ka live status: kya chal raha hai, volume, queue.", ".vcinfo"),
    "tag": ([], ".tag <name>  (audio reply karke)", "Audio ko naam se save karta hai.", ".tag intro"),
    "tags": ([], ".tags", "Aapke saare saved tags.", ".tags"),
    "untag": ([], ".untag <name>", "Saved tag delete karta hai.", ".untag intro"),
    "volume": (["vol"], ".volume <0-1000>", "Playback volume.", ".vol 800"),
    "gain": ([], ".gain <0-400>", "Loudness gain.", ".gain 150"),
    "bass": ([], ".bass <0-100>", "Bass level.", ".bass 20"),
    "treble": ([], ".treble <0-100>", "Awaaz ki clarity/sharpness.", ".treble 70"),
    "voice": ([], ".voice female|male|normal", "Voice profile badalta hai.", ".voice male"),
    "boost": ([], ".boost <0-10>", "Loudness stage.", ".boost 10"),
    "echo": ([], ".echo on|off", "Echo effect on/off.", ".echo off"),
    "echolvl": ([], ".echolvl <0-10>", "Echo kitna heavy ho.", ".echolvl 3"),
    "max": (["ultra"], ".max", "Sab setting maximum loudness par.", ".max"),
    "reset": ([], ".reset", "Saari audio settings default par.", ".reset"),
    "loud": ([], ".loud <0-18>", "Extra loudness layer.", ".loud 10"),
    "relaystatus": ([], ".relaystatus", "Current relay/audio settings dikhata hai.", ".relaystatus"),
    "playmute": (["unmuteaudio"], ".playmute  |  .playmute 3  |  .playmute loop",
                 "Admin unmute karte hi ye audio bajega (reply karke set karein).", "Audio reply karke: .playmute"),
    "handraise": ([], ".handraise on|off|now", "Mute hote hi hand raise karta hai.", ".handraise on"),
    "micblink": ([], ".micblink on|off|<sec>", "Unmute ke baad pehle mic on/off blink.", ".micblink 5"),
    "ss": ([], ".ss on  |  .ss off  |  photo reply karke .ss on",
           "VC me fake PC mixer screen share — live clock, LIVE timer aur chalte meters. "
           "Kitni bhi baar on/off kar sakte ho.", ".ss on"),
    "mic": ([], ".mic on [full|clean|bass|echo] [chat_id]\n.mic off  |  .mic leave  |  .mic status\n"
                ".mic src <private_chat_id>  |  .mic chat <target_chat_id>",
            "Live mic: aapki aawaz spare ID ke through target VC me loud + clear. "
            "Pehle .mic src aur .mic chat set karein.",
            ".mic chat -1001234567890\n.mic on"),
    "chat": ([], ".mic chat <target_chat_id>",
             "Live mic ka target group set karta hai (jahan aapki aawaz jayegi). "
             "ID -100 se shuru honi chahiye.", ".mic chat -1001234567890"),
    "src": ([], ".mic src <private_chat_id>", "Live mic ka source (private) group set karta hai.",
            ".mic src -1009876543210"),
    "micaccount": ([], ".micaccount", "Spare mic account ki jaankari / setup.", ".micaccount"),
    "spare": ([], ".spare join|mute|unmute|leave", "Spare ID ko VC me control karta hai.", ".spare join"),
    "myboost": (["livegain", "livevolume"], ".myboost <1-20000>", "Aapke account ki live mic gain.", ".myboost 500"),
    "bridge": ([], ".bridge", "VC bridge status / control.", ".bridge"),
    "setgc": (["unsetgc", "gc"], ".setgc <chat_id>  |  .unsetgc  |  .gc",
              "Default group set karta hai, taki DM se bina ID ke commands chalein.", ".setgc -1001234567890"),
    "vcchat": (["vcmsg", "vcm"], ".vcchat <text>", "VC wale group me message bhejta hai.", ".vcm hello"),
    "loopmsg": (["lmsg"], ".loopmsg 30 <text>  |  .loopmsg off", "Har N sec VC chat me wahi message bhejta hai.", ".loopmsg 30 Hello sab"),
    "schedule": (["smsg"], ".schedule 10m <text>  |  .schedule 21:30 <text>  |  .schedule list/off", "Time pe VC chat me message bhejta hai (IST).", ".schedule 10m Fight shuru"),
    "vcreact": (["vcr", "vcemoji"], ".vcreact <emoji>", "VC me emoji reaction.", ".vcr 🔥"),
    "audio": (["audios", "myaudio"], ".audio", "Audio library (My Audio / Bot Audios).", ".audio"),
    "saveaudio": ([], ".saveaudio <name>  (audio reply karke)", "Library me audio save.", ".saveaudio My Intro"),
    "addaudio": ([], "/addaudio <name>  (owner)", "Sabke liye shared audio add (owner only).", "/addaudio Welcome"),
    "setlog": ([], "/setlog <channel_id>  (owner)", "Log channel set.", "/setlog -1001234567890"),
    "logtest": ([], "/logtest", "Log channel test message.", "/logtest"),
}

ALIAS = {}
for _name, (_aliases, *_rest) in HELP.items():
    ALIAS[_name] = _name
    for _a in _aliases:
        ALIAS[_a] = _name


def help_text(word: str) -> str:
    key = ALIAS.get(word.lower().lstrip("./!"))
    if not key:
        return ""
    aliases, usage, desc, example = HELP[key]
    alias_line = ("\n<b>Aliases:</b> " + ", ".join(f"<code>.{a}</code>" for a in aliases)) if aliases else ""
    return (f"ℹ️ <b>.{key}</b>\n\n{desc}{alias_line}\n\n"
            f"<b>Kaise use karein:</b>\n<code>{usage}</code>\n\n"
            f"<b>Example:</b>\n<code>{example}</code>\n\n"
            "<i>Dot (.), slash (/) aur ! teeno chalte hain.</i>")


def _suggest(word: str) -> str:
    match = difflib.get_close_matches(word.lower(), list(ALIAS), n=3, cutoff=0.6)
    return ", ".join(f"<code>.{m}</code>" for m in match)


_HELP_ARG = re.compile(r"^[./!]([a-zA-Z]+)(?:@\w+)?\s+(help|\?|info|-h|--help)\s*$", re.I)


def _help_arg_filter(_, __, m):
    return bool(_HELP_ARG.match((m.text or "").strip()))


# group=-2: runs before the real command handlers so ".mic help" never
# accidentally starts the mic.
@Client.on_message(HAS_USER & filters.text & filters.create(_help_arg_filter), group=-2)
async def cmd_inline_help(bot: Client, msg: Message):
    word = _HELP_ARG.match(msg.text.strip()).group(1)
    text = help_text(word)
    if text:
        await msg.reply_text(text, disable_web_page_preview=True)
        msg.stop_propagation()


_HELP_CMD = re.compile(r"^[./!]help(?:@\w+)?\s+(\S+)", re.I)


@Client.on_message(HAS_USER & filters.text & filters.regex(_HELP_CMD), group=-2)
async def cmd_help_topic(bot: Client, msg: Message):
    word = _HELP_CMD.match(msg.text.strip()).group(1)
    text = help_text(word)
    if not text:
        sug = _suggest(word.lstrip("./!"))
        text = f"❓ <code>{word}</code> naam ki command nahi mili." + (f"\nShayad: {sug}" if sug else "") \
            + "\nSaari commands: <code>.help</code> → All Commands"
    await msg.reply_text(text, disable_web_page_preview=True)
    msg.stop_propagation()


_KNOWN_EXTRA = {"start", "login", "logout", "mystatus", "settings", "help", "redeem",
                "referral", "owner", "users", "stats", "broadcast", "restart", "ban",
                "unban", "setsource", "clearsource", "setprice", "grant", "revoke",
                "pending", "addcoupon", "delcoupon", "listcoupons", "setqr", "clearqr",
                "addfsub", "delfsub", "listfsub", "clearfsub", "fyt", "fight"}
_DOT_WORD = re.compile(r"^\.([a-zA-Z]{2,15})\b")


def _unknown_filter(_, __, m):
    mt = _DOT_WORD.match((m.text or "").strip())
    if not mt:
        return False
    w = mt.group(1).lower()
    return w not in ALIAS and w not in _KNOWN_EXTRA


# Only in bot DM, so normal group chat starting with "." is never spammed.
@Client.on_message(HAS_USER & filters.private & filters.text & filters.create(_unknown_filter), group=5)
async def cmd_unknown(bot: Client, msg: Message):
    word = _DOT_WORD.match(msg.text.strip()).group(1)
    sug = _suggest(word)
    await msg.reply_text(
        f"❓ <code>.{word}</code> command nahi hai."
        + (f"\nShayad aap ye chahte the: {sug}" if sug else "")
        + "\nKisi bhi command ki info: <code>.help mic</code> ya <code>.mic help</code>"
    )
