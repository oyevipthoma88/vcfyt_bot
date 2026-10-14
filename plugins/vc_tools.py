"""VC Chat panel: quick messages, reactions, custom text, loop + schedule.

Opened from the Now Playing screen ("💬 VC Chat + Reactions").
Commands:
  .loopmsg 30 Hello sab     -> har 30 sec VC chat me "Hello sab"
  .loopmsg off              -> loop band
  .schedule 10m Fight shuru -> 10 minute baad VC chat me message
  .schedule 21:30 Good night-> aaj 9:30 PM (IST) pe
  .schedule list | .schedule off
"""
import asyncio
import html
import re
import time
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K

from helpers.vc_manager import session_manager
from plugins.ui import B, HAS_USER, LINE, cmd_prefix, cmd_text, edit_screen, safe_answer

IST = timezone(timedelta(hours=5, minutes=30))
QUICK_MSGS = ["Hello sab 👋", "Awaaz aa rahi? 🎧", "GG 🔥", "Mic on karo 🎤",
              "Chup 🤫", "😂😂😂"]
REACTIONS = ["🔥", "❤️", "😂", "👏", "😮", "💯", "👍", "🤡"]

_loops: dict = {}       # (uid, cid) -> {"task", "text", "every"}
_scheds: dict = {}      # uid -> list of {"task", "cid", "text", "at"}
_typing: dict = {}      # uid -> cid  (next DM text goes to VC chat)


async def _send(uid: int, cid: int, text: str) -> str:
    uvc = await session_manager.get(uid)
    if not uvc:
        return "Pehle login karo."
    try:
        await uvc.send_vc_message(cid, text)
        return ""
    except Exception as exc:
        return str(exc)[:200] or type(exc).__name__


def _default_cid(uid: int):
    uvc = session_manager.users.get(uid)
    if not uvc:
        return None
    for cid, st in uvc.chats.items():
        if st.is_playing or getattr(st, "mic_enabled", False):
            return cid
    try:
        from plugins.vc_commands import DEFAULT_GC
        return DEFAULT_GC.get(uid)
    except Exception:
        return None


def panel(uid: int, cid: int):
    lp = _loops.get((uid, cid))
    sc = [x for x in _scheds.get(uid, []) if x["cid"] == cid and not x["task"].done()]
    text = (
        "💬 <b>VC Chat + Reactions</b>\n"
        f"{LINE}\n"
        "Button dabao → turant VC chat me jayega.\n"
        "Emoji = VC me <b>floating reaction</b>.\n\n"
        f"🔁 <b>Loop msg:</b> {('ON — har ' + str(lp['every']) + 's: ' + html.escape(lp['text'])) if lp else 'OFF'}\n"
        f"⏰ <b>Scheduled:</b> {len(sc)}\n"
        f"{LINE}\n"
        "Loop: <code>.loopmsg 30 Hello</code> · Band: <code>.loopmsg off</code>\n"
        "Schedule: <code>.schedule 10m Fight shuru</code> ya <code>.schedule 21:30 GN</code>"
    )
    rows = []
    for i in range(0, len(REACTIONS), 4):
        rows.append([B(e, callback_data=f"vct:r:{cid}:{i + j}", style="primary")
                     for j, e in enumerate(REACTIONS[i:i + 4])])
    rows.append([B("🔥 x5 Reaction Blast", callback_data=f"vct:blast:{cid}", style="success")])
    for i in range(0, len(QUICK_MSGS), 2):
        rows.append([B(m, callback_data=f"vct:q:{cid}:{i + j}", style="primary")
                     for j, m in enumerate(QUICK_MSGS[i:i + 2])])
    rows.append([B("✍️ Apna message likho", callback_data=f"vct:type:{cid}", style="success")])
    rows.append([B("⏹ Loop band" if lp else "🔁 Loop: last msg / 30s",
                   callback_data=f"vct:loop:{cid}", style="danger" if lp else "primary"),
                 B("⏰ Schedule hatao", callback_data=f"vct:sclr:{cid}", style="danger")])
    rows.append([B("⬅ Now Playing", callback_data=f"vc:now:{cid}", style="primary"),
                 B("🏠 Home", callback_data="menu:home", style="primary")])
    return text, K(rows)


_last_text: dict = {}


def _start_loop(uid, cid, every, text):
    _stop_loop(uid, cid)

    async def run():
        fails = 0
        while True:
            err = await _send(uid, cid, text)
            fails = fails + 1 if err else 0
            if fails >= 3:
                break
            await asyncio.sleep(every)
        _loops.pop((uid, cid), None)

    _loops[(uid, cid)] = {"task": asyncio.create_task(run()), "text": text, "every": every}


def _stop_loop(uid, cid=None):
    keys = [k for k in _loops if k[0] == uid and (cid is None or k[1] == cid)]
    for k in keys:
        _loops.pop(k)["task"].cancel()
    return len(keys)


@Client.on_callback_query(filters.regex(r"^vct:"))
async def cb_vct(bot, cq):
    parts = cq.data.split(":")
    action, cid = parts[1], int(parts[2])
    uid = cq.from_user.id
    if action == "panel":
        t, kb = panel(uid, cid)
        await edit_screen(cq.message, t, reply_markup=kb)
        return await safe_answer(cq)
    if action in ("r", "q"):
        src = REACTIONS if action == "r" else QUICK_MSGS
        text = src[int(parts[3]) % len(src)]
        err = await _send(uid, cid, text)
        if not err:
            _last_text[(uid, cid)] = text
        return await safe_answer(cq, f"❌ {err}" if err else f"✅ Bheja: {text}", show_alert=bool(err))
    if action == "blast":
        await safe_answer(cq, "🔥 Blast bhej raha hoon…")
        for i in range(5):
            if await _send(uid, cid, REACTIONS[i % 4]):
                break
            await asyncio.sleep(0.7)
        return
    if action == "type":
        _typing[uid] = cid
        await safe_answer(cq)
        return await cq.message.reply_text(
            "✍️ Ab apna message yaha likho — wo seedha VC chat me jayega.\n"
            "Band karne ke liye <code>cancel</code> likho.")
    if action == "loop":
        if (uid, cid) in _loops:
            _stop_loop(uid, cid)
            await safe_answer(cq, "⏹ Loop band")
        else:
            text = _last_text.get((uid, cid)) or QUICK_MSGS[0]
            _start_loop(uid, cid, 30, text)
            await safe_answer(cq, f"🔁 Har 30s: {text}")
    elif action == "sclr":
        n = 0
        for x in _scheds.get(uid, []):
            if x["cid"] == cid and not x["task"].done():
                x["task"].cancel()
                n += 1
        await safe_answer(cq, f"⏰ {n} schedule hataye")
    t, kb = panel(uid, cid)
    try:
        await edit_screen(cq.message, t, reply_markup=kb)
    except Exception:
        pass


def _is_typing(_, __, m):
    return bool(m.from_user and m.from_user.id in _typing and m.text
                and not m.text.startswith((".", "/", "!")))


@Client.on_message(HAS_USER & filters.private & filters.text & filters.create(_is_typing), group=-1)
async def typed_to_vc(bot, msg):
    uid = msg.from_user.id
    cid = _typing.get(uid)
    if msg.text.strip().lower() == "cancel":
        _typing.pop(uid, None)
        await msg.reply_text("✅ Typing mode band.")
        msg.stop_propagation()
    err = await _send(uid, cid, msg.text)
    if err:
        await msg.reply_text(f"❌ VC chat nahi gaya: <code>{html.escape(err)}</code>")
    else:
        _last_text[(uid, cid)] = msg.text
        await msg.reply_text("✅ VC chat me gaya. Aur likho, ya <code>cancel</code>.")
    msg.stop_propagation()


def _pick_chat(msg, words):
    if words and re.fullmatch(r"-100\d+", words[-1]):
        return int(words[-1]), words[:-1]
    if msg.chat and msg.chat.id < 0:
        return msg.chat.id, words
    return _default_cid(msg.from_user.id), words


@Client.on_message(HAS_USER & cmd_prefix(r"(loopmsg|lmsg)\b", flags=re.IGNORECASE))
async def cmd_loopmsg(bot, msg):
    uid = msg.from_user.id
    words = cmd_text(msg).split()[1:]
    if words and words[0].lower() in ("off", "stop"):
        n = _stop_loop(uid)
        return await msg.reply_text(f"⏹ {n} loop message band.")
    if len(words) < 2 or not words[0].isdigit():
        return await msg.reply_text(
            "🔁 <b>Loop message</b>\n<code>.loopmsg 30 Hello sab</code> = har 30 sec\n"
            "<code>.loopmsg off</code> = band (min 10 sec)")
    every = max(10, min(3600, int(words[0])))
    cid, words = _pick_chat(msg, words[1:])
    if not cid:
        return await msg.reply_text("❌ Group nahi mila. Group me likho ya end me -100… ID do.")
    text = " ".join(words)
    _start_loop(uid, cid, every, text)
    await msg.reply_text(f"🔁 Loop ON: har <b>{every}s</b> VC chat me → {html.escape(text)}\n"
                         "Band: <code>.loopmsg off</code>")


def _parse_when(tok: str):
    m = re.fullmatch(r"(\d+)([smh])", tok.lower())
    if m:
        n, u = int(m.group(1)), m.group(2)
        return time.time() + n * {"s": 1, "m": 60, "h": 3600}[u]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", tok)
    if m:
        now = datetime.now(IST)
        at = now.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)) % 60,
                         second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return at.timestamp()
    return None


@Client.on_message(HAS_USER & cmd_prefix(r"(schedule|smsg)\b", flags=re.IGNORECASE))
async def cmd_schedule(bot, msg):
    uid = msg.from_user.id
    words = cmd_text(msg).split()[1:]
    lst = [x for x in _scheds.get(uid, []) if not x["task"].done()]
    _scheds[uid] = lst
    if words and words[0].lower() == "list":
        if not lst:
            return await msg.reply_text("⏰ Koi schedule nahi.")
        lines = [f"• {datetime.fromtimestamp(x['at'], IST):%d-%m %I:%M %p} → {html.escape(x['text'])}"
                 for x in lst]
        return await msg.reply_text("⏰ <b>Scheduled</b>\n" + "\n".join(lines))
    if words and words[0].lower() in ("off", "clear", "stop"):
        for x in lst:
            x["task"].cancel()
        _scheds[uid] = []
        return await msg.reply_text(f"⏰ {len(lst)} schedule hata diye.")
    at = _parse_when(words[0]) if words else None
    if not at or len(words) < 2:
        return await msg.reply_text(
            "⏰ <b>Schedule message</b>\n<code>.schedule 10m Fight shuru</code>\n"
            "<code>.schedule 21:30 Good night</code> (IST)\n"
            "<code>.schedule list</code> · <code>.schedule off</code>")
    cid, words = _pick_chat(msg, words[1:])
    if not cid:
        return await msg.reply_text("❌ Group nahi mila. Group me likho ya end me -100… ID do.")
    text = " ".join(words)

    async def run():
        await asyncio.sleep(max(0, at - time.time()))
        err = await _send(uid, cid, text)
        try:
            await bot.send_message(uid, f"⏰ Scheduled msg {'❌ fail: ' + html.escape(err) if err else '✅ VC chat me gaya'}: {html.escape(text)}")
        except Exception:
            pass

    lst.append({"task": asyncio.create_task(run()), "cid": cid, "text": text, "at": at})
    await msg.reply_text(f"⏰ Set: <b>{datetime.fromtimestamp(at, IST):%d-%m %I:%M %p}</b> IST → "
                         f"{html.escape(text)}")
