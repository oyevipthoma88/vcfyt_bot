"""
BANALL BOT — Simple.
Bot ko group me admin (Ban Users ON) banao -> koi bhi group admin /banall bheje ->
bot saare non-admin members + bots ko remove kar dega. Admins ignore.

No JSON, no cache, no userbot. Sirf bot + admin rights.
"""
import asyncio, os, string, time

import termux_env
termux_env.bootstrap()

from telethon import TelegramClient, Button, events
from telethon.errors import (
    FloodWaitError, UserAdminInvalidError, ChatAdminRequiredError,
)
from telethon.tl.functions.channels import (
    GetParticipantsRequest, EditBannedRequest, GetParticipantRequest,
)
from telethon.tl.types import (
    ChannelParticipantsRecent, ChannelParticipantsAdmins,
    ChannelParticipantsSearch, ChatBannedRights,
    ChannelParticipantAdmin, ChannelParticipantCreator,
)

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "").lstrip("@")
TUTORIAL_URL = os.environ.get("TUTORIAL_URL", "https://t.me/")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "48"))

if not (API_ID and API_HASH and BOT_TOKEN):
    print("Missing API_ID / API_HASH / BOT_TOKEN. Delete .env and re-run.")
    raise SystemExit(1)

BAN = ChatBannedRights(until_date=None, view_messages=True)
UNBAN = ChatBannedRights(until_date=None, view_messages=False)

bot = TelegramClient("banall_bot", API_ID, API_HASH)
stop_flags: dict[int, bool] = {}


def main_menu():
    rows = [[Button.url("📖 Tutorial", TUTORIAL_URL)]]
    if OWNER_USERNAME:
        rows.append([Button.url("👑 Owner", f"https://t.me/{OWNER_USERNAME}")])
    return rows


@bot.on(events.NewMessage(pattern=r"^/start"))
async def start(e):
    txt = (
        "👋 **BANALL BOT**\n\n"
        "**Steps:**\n"
        "1. Mujhe group me add karo.\n"
        "2. **Admin** banao with **Ban Users** permission ON.\n"
        "3. Koi bhi group **admin** `/banall` bheje — sab non-admin members + bots ban ho jayenge.\n\n"
        "**Commands (group ke andar, sirf admin use kar sakta hai):**\n"
        "`/banall`  — sab non-admin members + bots ban\n"
        "`/kickall` — sab non-admin members + bots kick (unban)\n"
        "`/stop`    — running task rok do\n"
        "`/alive`   — status\n"
    )
    await e.reply(txt, buttons=main_menu(), link_preview=False)


# ---------------- Admin check ----------------
async def is_group_admin_with_ban(chat, user_id: int) -> bool:
    """True if user is admin/creator with ban rights."""
    try:
        p = await bot(GetParticipantRequest(chat, user_id))
        part = p.participant
        if isinstance(part, ChannelParticipantCreator):
            return True
        if isinstance(part, ChannelParticipantAdmin):
            rights = part.admin_rights
            return bool(rights and rights.ban_users)
    except Exception:
        pass
    return False


async def collect_admin_ids(chat) -> set[int]:
    a = set()
    try:
        async for u in bot.iter_participants(chat, filter=ChannelParticipantsAdmins()):
            a.add(u.id)
    except Exception:
        pass
    return a


# ---------------- Enumerate all participants (as bot admin) ----------------
async def enumerate_members(chat) -> set[int]:
    ids: set[int] = set()

    async def pull(filt):
        off = 0
        while True:
            try:
                r = await bot(GetParticipantsRequest(chat, filt, off, 200, hash=0))
            except FloodWaitError as ex:
                await asyncio.sleep(ex.seconds + 1); continue
            except Exception:
                break
            if not r.users:
                break
            for u in r.users:
                ids.add(u.id)
            off += len(r.users)
            if len(r.users) < 200:
                break

    await pull(ChannelParticipantsRecent())
    # Aggregator search a-z 0-9 to defeat 10k cap
    for ch in string.ascii_lowercase + string.digits:
        await pull(ChannelParticipantsSearch(ch))
    return ids


# ---------------- Ban engine ----------------
async def ban_one(chat, uid, rights, sem, ctr):
    async with sem:
        for _ in range(3):
            try:
                await bot(EditBannedRequest(chat, uid, rights))
                ctr["ok"] += 1; return
            except FloodWaitError as ex:
                await asyncio.sleep(min(ex.seconds, 5) + 0.1)
            except (UserAdminInvalidError, ChatAdminRequiredError):
                ctr["skip"] += 1; return
            except Exception:
                ctr["fail"] += 1; return
        ctr["fail"] += 1


async def action(e, mode: str):
    if not (e.is_group or e.is_channel):
        await e.reply("Group ke andar use karo."); return

    chat = await e.get_chat()

    # Only group admins with ban rights can trigger
    if not await is_group_admin_with_ban(chat, e.sender_id):
        await e.reply("❌ Sirf group ke admin (ban rights ke sath) trigger kar sakte hain."); return

    # Bot must be admin with ban rights
    me = await bot.get_me()
    if not await is_group_admin_with_ban(chat, me.id):
        await e.reply("❌ Mujhe pehle group me **admin + Ban Users** permission do."); return

    status = await e.reply(f"[{mode}] Members collect kar raha hun...")

    admins = await collect_admin_ids(chat)
    all_ids = await enumerate_members(chat)
    protect = admins | {me.id}
    if OWNER_ID:
        protect.add(OWNER_ID)

    targets = [u for u in all_ids if u not in protect]
    total = len(targets)
    if not total:
        await status.edit("Kuch nahi karna. (Bot ko admin banaya? Group me members hain?)"); return

    stop_flags[e.chat_id] = False
    ctr = {"ok": 0, "fail": 0, "skip": 0}
    sem = asyncio.Semaphore(CONCURRENCY)
    rights = UNBAN if mode == "kickall" else BAN
    t0 = time.time()

    async def prog():
        while not stop_flags.get(e.chat_id):
            done = ctr["ok"] + ctr["fail"] + ctr["skip"]
            if done >= total: break
            try:
                await status.edit(
                    f"[{mode}] {done}/{total} ok:{ctr['ok']} fail:{ctr['fail']} "
                    f"{done/(time.time()-t0+0.01):.1f}/s"
                )
            except Exception: pass
            await asyncio.sleep(3)

    p = asyncio.create_task(prog())
    tasks = [ban_one(chat, uid, rights, sem, ctr) for uid in targets]
    CHUNK = 2000
    for i in range(0, len(tasks), CHUNK):
        if stop_flags.get(e.chat_id): break
        await asyncio.gather(*tasks[i:i+CHUNK])
    stop_flags[e.chat_id] = True
    p.cancel()

    dur = time.time() - t0
    await status.edit(
        f"✅ [{mode}] {dur:.1f}s ok:{ctr['ok']} fail:{ctr['fail']} skip:{ctr['skip']} "
        f"({ctr['ok']/max(dur,0.01):.1f}/s)"
    )


@bot.on(events.NewMessage(pattern=r"^/banall(?:@\w+)?$"))
async def _(e): await action(e, "banall")

@bot.on(events.NewMessage(pattern=r"^/kickall(?:@\w+)?$"))
async def _(e): await action(e, "kickall")

@bot.on(events.NewMessage(pattern=r"^/stop(?:@\w+)?$"))
async def _(e):
    if not (e.is_group or e.is_channel): return
    chat = await e.get_chat()
    if not await is_group_admin_with_ban(chat, e.sender_id): return
    stop_flags[e.chat_id] = True
    await e.reply("Stopping...")

@bot.on(events.NewMessage(pattern=r"^/alive(?:@\w+)?$"))
async def _(e):
    await e.reply(f"BANALL alive | concurrency={CONCURRENCY}")


async def main():
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"BANALL BOT online as @{me.username} | concurrency={CONCURRENCY}")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
