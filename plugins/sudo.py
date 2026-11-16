"""Sudo flow UI: free trial, verified group link, sudo requests, owner panel.

User side (bina login):
  /start -> 🎁 Bina Login Use Karo -> Free Trial (3 uses)
  -> group link bhejo -> Sudo ID join + verify (group, member, VC)
  -> ▶️ Play / 🎤 Live Mic
  Trial khatam -> apna Login | 5 referrals = Premium | Support GC -> Sudo request

Owner side: Owner Panel -> 👑 Sudo
  Sudo ID add (string ya phone+OTP), list/remove, sudo users,
  trial uses, support GC, requests approve/reject.
"""
import asyncio
import html
import logging
import re
import time
from types import SimpleNamespace

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K, Message

from config import Config
from helpers import sudo
from helpers.access_control import format_until
from helpers.database import db
from plugins.ui import B, HAS_USER, LINE, card, cmd_text, dm_cmd, edit_screen, safe_answer

logger = logging.getLogger("vcbot.sudo.ui")

_AWAIT_TTL = 600
_await: dict = {}          # uid -> {"kind": str, "ts": float, ...}
_logins: dict = {}         # owner uid -> pending pyrogram login


def _esc(v) -> str:
    return html.escape(str(v or ""))


def _set_await(uid: int, kind: str, **extra) -> None:
    _await[int(uid)] = {"kind": kind, "ts": time.time(), **extra}


def _get_await(uid: int):
    st = _await.get(int(uid))
    if st and time.time() - st["ts"] > _AWAIT_TTL:
        _await.pop(int(uid), None)
        return None
    return st


async def _ref_progress(uid: int) -> tuple[int, int]:
    s = await sudo.settings()
    try:
        count = await db.count_referrals(uid)
    except Exception:
        count = 0
    return count, int(s["ref_needed"])


# ══════════════════════════ USER SCREENS ══════════════════════════════

def _options_rows(s: dict, bot_username: str = "") -> list:
    rows = [[B("⚔️ Apni Fight ID jodo (unlimited)", callback_data="menu:login", style="success")],
            [B("👥 Refer 5 = Premium", callback_data="ref:menu", style="success")]]
    sup = sudo.support_url(s)
    if sup:
        rows.append([B("🆘 Support GC Join", url=sup),
                     B("🙋 Sudo Request", callback_data="sudo:req", style="primary")])
    else:
        rows.append([B("🙋 Owner se Sudo Request", callback_data="sudo:req", style="primary")])
    return rows


async def paywall_screen(uid: int) -> tuple[str, K]:
    mode = await sudo.engine_mode(uid, fresh=True)
    s = await sudo.settings()
    t = await sudo.trial_state(uid)
    refs, need = await _ref_progress(uid)
    gc_id, _, gc_title = await sudo.get_gc(uid)
    gc_line = (f"🎯 <b>Group:</b> {_esc(gc_title) or gc_id} ✅" if gc_id
               else "🎯 <b>Group:</b> abhi set nahi")
    use_rows = [[B("🎯 Group Link Bhejo" if not gc_id else "🔁 Group Badlo",
                   callback_data="sudo:gc", style="success")]]
    if gc_id:
        use_rows.append([B("▶️ Audio Play", callback_data="sudo:play", style="success"),
                         B("🎤 Live Mic", callback_data="sudo:mic", style="success")])

    if mode in (sudo.OWNER, sudo.SELF):
        return (card("✅", "Login active", "Aap apne account se bot chala rahe ho — Sudo ki zarurat nahi."),
                K([[B("🏠 Home", callback_data="menu:home", style="primary")]]))

    if mode == sudo.NONE:
        body = (f"Login ke bina bot try karo — <b>{t['limit']} free uses</b>.\n\n"
                "① <b>Free Trial</b> dabao\n"
                "② Apne group ka link bhejo (<code>t.me/...</code> ya <code>@username</code>)\n"
                "③ Sudo ID group me aa kar verify karegi\n"
                "④ <b>.play</b> ya <b>🎤 Live Mic</b> — jo chahiye\n\n"
                f"{LINE}\nTrial ke baad: apni ⚔️ Fight ID, ya <b>{need} referrals</b> = Premium, "
                "ya Support GC join karke owner se Sudo lo.")
        rows = [[B(f"🎁 Free Trial Start · {t['limit']} uses", callback_data="sudo:trial", style="success")]]
        rows += _options_rows(s)
        rows.append([B("🏠 Home", callback_data="menu:home", style="primary")])
        return card("🎁", "Bina login use karo", body), K(rows)

    if mode == sudo.TRIAL:
        body = (f"🎁 <b>Trial:</b> {t['left']}/{t['limit']} uses baaki\n{gc_line}\n\n"
                "Har <b>.play</b> / <b>Live Mic ON</b> = 1 use.\n"
                f"👥 Referrals: <b>{refs}/{need}</b> — {need} hote hi Premium (unlimited).")
        rows = use_rows + _options_rows(s)
        rows.append([B("🏠 Home", callback_data="menu:home", style="primary")])
        return card("🎁", "Free trial active", body), K(rows)

    if mode == sudo.TRIAL_OVER:
        body = (f"Aapke <b>{t['limit']} free uses</b> khatam ho gaye.\n\n"
                "Aage use karne ke 3 tareeke:\n"
                "① <b>⚔️ Apni Fight ID</b> jodo — unlimited Live Mic\n"
                f"② <b>{need} dosto ko refer</b> karo — Premium active (bina login unlimited)\n"
                f"   👥 Abhi: <b>{refs}/{need}</b>\n"
                "③ <b>Support GC join</b> karo aur owner se <b>Sudo</b> lo")
        rows = _options_rows(s)
        rows.append([B("💎 Premium Plans", callback_data="pay:menu", style="success"),
                     B("🏠 Home", callback_data="menu:home", style="primary")])
        return card("⛔", "Trial khatam", body), K(rows)

    # SUDO / PREMIUM
    if mode == sudo.SUDO:
        status = f"👑 <b>Sudo active</b> · till {format_until(await sudo.sudo_until(uid))}"
    else:
        from helpers.access_control import premium_info
        p = await premium_info(uid)
        status = f"💎 <b>Premium active</b> · till {format_until(p.get('until'))}"
    body = f"{status}\n{gc_line}\n\nBina login — owner ki Sudo ID se <b>unlimited</b> .play + Live Mic."
    rows = use_rows + [[B("🏠 Home", callback_data="menu:home", style="primary")]]
    return card("👑", "Sudo access", body), K(rows)


async def home_override(uid: int, name: str = "", notice: str = ""):
    """Home screen for users without a Main ID login (start.py).

    v92 SIMPLE: ek hi main raasta — ⚔️ Fight ID jodo (wo ID jo VC me bolti hai).
    Main ID optional hai.  Fight ID jud gayi to seedha 🎙 Live Mic button.
    Owner ki Sudo IDs wala Free Trial sirf chhota secondary option hai."""
    from plugins.ui import source_button
    from helpers.setup_flow import has_fight_id
    mode = await sudo.engine_mode(uid, fresh=True)
    if mode in (sudo.OWNER, sudo.SELF):
        # Login ho chuka (cache purana tha) — normal home hi sahi hai.
        from plugins.ui import home_kb, home_text
        return home_text(name or "friend", True), home_kb(Config.is_owner(uid), True)
    fight = await has_fight_id(uid)
    t = await sudo.trial_state(uid)
    has_ids = bool(await sudo.accounts())
    refs, need = await _ref_progress(uid)
    gc_id, _, gc_title = await sudo.get_gc(uid)
    greeting = f"👋 Hey <b>{_esc(name or 'friend')}</b>"

    lines = [greeting, "🔊 Ultra-loud + clear Live Mic", ""]
    if notice:
        lines += [notice, ""]
    if fight:
        tgt = await db.get_app_value(f"mic_chat_{uid}")
        lines += ["✅ <b>Fight ID judi hui hai</b>",
                  f"{'✅' if tgt else '②'} <b>Group</b> {('<code>' + _esc(tgt) + '</code>') if tgt else '— Live Mic me set karo'}",
                  "③ <b>🎙 Live Mic</b> dabao → link Chrome me kholo → bolo",
                  "",
                  "👤 Main ID <b>zaroori nahi</b> (sirf Player / Library ke liye)."]
    else:
        lines += ["<b>Sirf 3 step:</b>",
                  "① <b>⚔️ Fight ID jodo</b> — wo ID jo VC me bolegi",
                  "② <b>🎯 Group batao</b> — jisme VC chal rahi hai",
                  "③ <b>🎙 Link kholo</b> (Chrome) aur bolo",
                  "",
                  "👤 Apni main ID dena <b>zaroori nahi</b>."]

    status = ""
    if mode == sudo.TRIAL:
        status = f"🎁 Trial active · <b>{t['left']}/{t['limit']}</b> uses baaki"
    elif mode == sudo.TRIAL_OVER and not fight:
        status = (f"⛔ Trial khatam · Fight ID jodo, ya <b>{need}</b> referrals = Premium "
                  f"(abhi {refs}/{need})")
    elif mode == sudo.SUDO:
        status = f"👑 Sudo active · till {format_until(await sudo.sudo_until(uid))}"
    elif mode == sudo.PREMIUM:
        from helpers.access_control import premium_info
        p = await premium_info(uid)
        status = f"💎 Premium active · till {format_until(p.get('until'))}"
    if status and gc_id and mode in (sudo.TRIAL, sudo.SUDO, sudo.PREMIUM):
        status += f"\n🎯 Group: {_esc(gc_title) or gc_id} ✅"
    if status:
        lines += ["", f"<b>Status</b>  {status}"]
    text = card("🎧", "Apex · VC Fight", "\n".join(lines),
                "⚔️ Fight ID = koi bhi doosri Telegram ID (number + OTP se judti hai). "
                "Use apne group me add rakho.")

    if fight:
        rows = [[B("🎙 Live Mic shuru karo", callback_data="mic:panel", style="success")],
                [B("⚔️ Fight ID", callback_data="mic:acct", style="primary"),
                 B("👤 Main ID (optional)", callback_data="menu:mainid", style="primary")]]
    else:
        rows = [[B("⚔️ Fight ID jodo  ·  Step 1", callback_data="menu:login", style="success")]]
        if mode == sudo.NONE and has_ids:
            rows.append([B(f"🎁 ID nahi hai? Free Trial · {t['limit']} uses",
                           callback_data="sudo:trial", style="primary")])
        elif mode == sudo.TRIAL:
            rows.append([B(f"🎁 Trial Panel  ·  {t['left']}/{t['limit']} baaki",
                           callback_data="sudo:home", style="primary")])
        elif mode in sudo.UNLIMITED_MODES:
            rows.append([B("👑 Sudo Panel  ·  Play / Live Mic", callback_data="sudo:home", style="success")])
        elif mode == sudo.TRIAL_OVER:
            rows.append([B("⛔ Trial khatam  ·  Options", callback_data="sudo:home", style="danger")])
    rows.append([B("🎬 Video Guide", callback_data="tut:video", style="primary"),
                 B("📘 Help", callback_data="menu:tutorial", style="primary")])
    rows.append([B("👥 Refer & Earn", callback_data="ref:menu", style="primary"),
                 B("💎 Premium", callback_data="pay:menu", style="primary")])
    rows.extend(source_button())
    return text, K(rows)


async def no_engine_notice(msg) -> None:
    from plugins.vc_commands import mic_notify
    uid = msg.from_user.id
    mode = await sudo.engine_mode(uid, fresh=True)
    if mode in sudo.SUDO_ENGINE_MODES:
        if not await sudo.accounts():
            text = ("⚠️ <b>Abhi koi Sudo ID add nahi hai</b>\n\n"
                    "Owner ne Sudo ID set nahi ki. Thodi der baad try karo ya apni ⚔️ Fight ID jodo.")
        elif await sudo.all_busy(uid):
            text = ("⏳ <b>Saari Sudo IDs abhi dusre groups me busy hain</b>\n\n"
                    "1-2 minute baad try karo, ya apni ⚔️ Fight ID jodo (kabhi wait nahi).")
        else:
            text = ("⏳ <b>Sudo ID connect ho rahi hai…</b>\n\n"
                    "10-20 second baad dobara try karo. Baar-baar aaye to Support GC me batao.")
        await mic_notify(msg, text, reply_markup=K([[B("⚔️ Fight ID jodo", callback_data="menu:login")]]))
        return
    if mode == sudo.NONE:
        text, kb = await home_override(uid, getattr(msg.from_user, "first_name", ""),
                                       notice="🔒 <b>Iske liye ⚔️ Fight ID (ya Free Trial) chahiye.</b>")
    else:
        text, kb = await paywall_screen(uid)
    await mic_notify(msg, text, reply_markup=kb)


async def _ask_gc(target, uid: int, edit: bool = False):
    _set_await(uid, "gc")
    text = card("🎯", "Group link bhejo",
                "Jis group me VC chalana hai uska link yahin bhejo:\n"
                "• <code>https://t.me/+AbCdEf…</code> (private invite)\n"
                "• <code>https://t.me/groupname</code> ya <code>@groupname</code>\n\n"
                "Sudo ID group join karegi aur verify karegi ki:\n"
                "✓ ye group hai  ✓ aap member ho  ✓ VC status",
                "Prompt 10 minute active. Group me 'join request' ON ho to Sudo ID ki request approve karna.")
    kb = K([[B("✖ Cancel", callback_data="sudo:cancel", style="danger")]])
    if edit:
        await edit_screen(target, text, reply_markup=kb)
    else:
        await target.reply_text(text, reply_markup=kb)


async def gate_sudo_use(msg) -> bool:
    """Called before every .play / Live Mic ON for non-logged users.
    Group verified hona chahiye; trial ho to 1 use katta hai."""
    from plugins.vc_commands import mic_notify
    uid = msg.from_user.id
    mode = await sudo.engine_mode(uid, fresh=True)
    if mode in (sudo.OWNER, sudo.SELF):
        return True
    if mode == sudo.NONE:
        text, kb = await home_override(uid, getattr(msg.from_user, "first_name", ""),
                                       notice="🔒 <b>Iske liye Login ya Free Trial chahiye.</b>")
        await mic_notify(msg, text, reply_markup=kb)
        return False
    if mode == sudo.TRIAL_OVER:
        text, kb = await paywall_screen(uid)
        await mic_notify(msg, text, reply_markup=kb)
        return False
    gc_id, _, _ = await sudo.get_gc(uid)
    if not gc_id:
        _set_await(uid, "gc")
        await mic_notify(msg, "🎯 <b>Pehle apne group ka link bhejo</b> (isi DM me).\n"
                              "Verify hote hi .play / Live Mic chalega.",
                         reply_markup=K([[B("✖ Cancel", callback_data="sudo:cancel", style="danger")]]))
        return False
    if mode in sudo.UNLIMITED_MODES:
        return True
    ok, st = await sudo.consume_trial(uid)
    if not ok:
        text, kb = await paywall_screen(uid)
        await mic_notify(msg, text, reply_markup=kb)
        return False
    note = (f"🎁 Trial use <b>{st['used']}/{st['limit']}</b>"
            + (" — ye aakhri free use tha." if st["left"] == 0 else f" · {st['left']} baaki"))
    try:
        await mic_notify(msg, note)
    except Exception:
        pass
    return True


# Backwards-compatible name used by vc_commands / web_mic
gate_trial_use = gate_sudo_use


async def refund_if_trial(uid: int) -> None:
    try:
        if await sudo.engine_mode(uid, fresh=True) in (sudo.TRIAL, sudo.TRIAL_OVER):
            await sudo.refund_trial(uid)
    except Exception:
        pass


# ══════════════════════════ GROUP VERIFY ══════════════════════════════

_INVITE_RE = re.compile(r"(?:t(?:elegram)?\.me/(?:joinchat/|\+)|^\+)([A-Za-z0-9_-]{6,})")
_PUBLIC_RE = re.compile(r"(?:t(?:elegram)?\.me/)(?!\+|joinchat/|c/)([A-Za-z][A-Za-z0-9_]{3,})")
_CLINK_RE = re.compile(r"t\.me/c/(\d{5,})")
_PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{6,18}$")


def _looks_like_group_ref(ref: str) -> bool:
    """Group link / @username / -100 chat id. Phone number ya OTP nahi."""
    ref = (ref or "").strip()
    if not ref or _PHONE_RE.match(ref) and not ref.startswith("-"):
        return False
    return bool(_INVITE_RE.search(ref) and not re.fullmatch(r"\+\d+", ref)
                or _PUBLIC_RE.search(ref) or _CLINK_RE.search(ref)
                or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,}", ref)
                or re.fullmatch(r"-\d{6,}", ref))


class _ConnLost(Exception):
    """Sudo ID ka Telegram connection beech me toot gaya (retry-able)."""


async def _resolve_join(client, ref: str):
    """Join + resolve the group with the sudo account. Returns (chat, join_ref, error)."""
    from pyrogram import errors as E
    ref = (ref or "").strip()
    inv = _INVITE_RE.search(ref)
    pub = _PUBLIC_RE.search(ref)
    clink = _CLINK_RE.search(ref)

    def err(name):
        return getattr(E, name, ())

    try:
        if inv:
            link = f"https://t.me/+{inv.group(1)}"
            try:
                chat = await client.join_chat(link)
            except err("UserAlreadyParticipant"):
                chat = await client.get_chat(link)
            return chat, link, ""
        if pub or ref.startswith("@"):
            name = pub.group(1) if pub else ref[1:]
            try:
                chat = await client.join_chat(name)
            except err("UserAlreadyParticipant"):
                chat = await client.get_chat(name)
            return chat, f"@{name}", ""
        raw = f"-100{clink.group(1)}" if clink else ref
        if re.fullmatch(r"-?\d{6,}", raw):
            from helpers.peer_guard import chat_id_variants
            for cand in chat_id_variants(raw):
                try:
                    return await client.get_chat(cand), None, ""
                except Exception:
                    continue
            return None, None, ("❌ Numeric ID se group nahi mila — Sudo ID us group me nahi hai.\n"
                                "Group ka <b>invite link</b> bhejo (Group → Invite Links).")
    except err("InviteRequestSent"):
        return None, None, ("⏳ Group me <b>join request</b> ON hai. Group admin Sudo ID ki request "
                            "approve kare, phir wahi link dobara bhejo.")
    except (err("InviteHashExpired"), err("InviteHashInvalid")):
        return None, None, "❌ Invite link galat ya expire hai. Naya link bhejo."
    except (err("UsernameNotOccupied"), err("UsernameInvalid")):
        return None, None, "❌ Is username ka koi group nahi mila."
    except err("UserBannedInChannel"):
        return None, None, "❌ Sudo ID is group me banned hai. Admin unban kare ya dusra group do."
    except err("ChannelsTooMuch"):
        return None, None, "❌ Sudo ID bahut groups me hai. Owner ko Support GC me batao."
    except err("FloodWait") as e:
        return None, None, f"⏳ Telegram limit — {getattr(e, 'value', 30)} sec baad try karo."
    except Exception as e:
        if sudo.is_conn_error(e):
            # verify_gc fresh Sudo ID connection ke saath dobara try karega
            raise _ConnLost(e) from e
        return None, None, f"❌ Group join/verify fail: <code>{_esc(e)}</code>"
    return None, None, "❌ Link samajh nahi aaya. <code>https://t.me/...</code> ya <code>@username</code> bhejo."


async def _is_member(client, chat_id: int, user) -> tuple[bool, str]:
    """(ok, note). Hard-fails only when Telegram clearly says 'not a member'."""
    from pyrogram import errors as E
    from pyrogram.enums import ChatMemberStatus as S
    for who in (user.id, user.username):
        if not who:
            continue
        try:
            m = await client.get_chat_member(chat_id, who)
            if m.status in (S.LEFT, S.BANNED):
                return False, "left"
            return True, ""
        except getattr(E, "UserNotParticipant", ()):
            return False, "not_member"
        except Exception:
            continue
    return True, "unverified"


async def verify_gc(bot, user, ref: str) -> tuple[bool, str, K]:
    uid = user.id
    retry_kb = K([[B("🔁 Dobara Link Bhejo", callback_data="sudo:gc", style="primary"),
                   B("🏠 Home", callback_data="menu:home", style="primary")]])
    if not _looks_like_group_ref(ref):
        return False, ("❌ Ye group link nahi lagta.\n"
                       "<code>https://t.me/+AbCd…</code>, <code>https://t.me/groupname</code> "
                       "ya <code>@groupname</code> bhejo."), retry_kb
    if not await sudo.accounts():
        return False, ("⚠️ <b>Abhi koi Sudo ID add nahi hai</b>\n"
                       "Owner ne Sudo ID set nahi ki. Tab tak apna 🔐 Login use karo."), retry_kb
    eng = chat = join_ref = None
    error = ""
    last_exc = None
    for attempt in range(3):
        eng = await sudo.engine_for(uid)
        if not eng or not getattr(eng, "client", None):
            if await sudo.all_busy(uid):
                return False, ("⏳ Saari Sudo IDs abhi dusre groups me busy hain. 1-2 min baad link "
                               "dobara bhejo, ya apna 🔐 Login karo."), retry_kb
            if attempt < 2 and last_exc is not None:
                await asyncio.sleep(2)
                continue
            return False, ("⚠️ Sudo ID abhi connect nahi ho payi. 30 second baad link dobara bhejo.\n"
                           "Baar-baar aaye to apna 🔐 Login karo ya Support GC me batao."), retry_kb
        try:
            chat, join_ref, error = await _resolve_join(eng.client, ref)
            sudo.mark_healthy(eng)
            last_exc = None
            break
        except _ConnLost as e:
            last_exc = e.__cause__ or e
            logger.warning("verify_gc uid=%s attempt=%s: sudo connection lost (%r) — reconnecting",
                           uid, attempt + 1, last_exc)
            # Mari hui engine hatao; agla engine_for() fresh connect karega.
            await sudo.drop_engine(sudo.engine_key(eng))
            await asyncio.sleep(1 + attempt)
    if last_exc is not None:
        try:
            from helpers.logger_channel import log_error
            await log_error("sudo_verify_gc_connection", last_exc)
        except Exception:
            pass
        return False, ("⚠️ <b>Sudo ID ka Telegram connection baar-baar toot raha hai.</b>\n"
                       "Maine 3 baar reconnect try kiya. 1 minute baad link dobara bhejo — "
                       "ya apna 🔐 Login karo (turant chalega)."), retry_kb
    if error or chat is None or not getattr(chat, "id", None):
        return False, error or "❌ Group verify nahi hua. Invite link bhejo.", retry_kb
    from pyrogram.enums import ChatType
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False, "❌ Ye group nahi hai (channel/user). VC wala <b>group</b> ka link bhejo.", retry_kb
    if not Config.is_owner(uid):
        ok, why = await _is_member(eng.client, chat.id, user)
        if not ok:
            return False, ("❌ <b>Aap is group ke member nahi ho.</b>\n"
                           "Sudo sirf usi group me chalegi jisme aap khud ho. Pehle group join karo."), retry_kb
    else:
        why = ""
    try:
        call = await eng._call_input(chat.id)
    except Exception:
        call = None
    acct_key = sudo.account_key(eng.string_session)
    await sudo.save_gc(uid, chat.id, join_ref or "", chat.title or "", acct_key)
    # Live Mic + default group bhi isi group par
    try:
        from plugins.live_mic_target import save_target, retarget_live
        await save_target(uid, chat.id, join_ref)
        await retarget_live(uid, chat.id, join_ref)
    except Exception:
        pass
    try:
        from plugins.vc_commands import DEFAULT_GC, _save_gc
        DEFAULT_GC[uid] = chat.id
        _save_gc()
    except Exception:
        pass
    try:
        await db.register_broadcast_chat(chat.id, chat.title or "")
    except Exception:
        pass
    sudo._cache_clear(uid)
    who = f"@{eng.account_username}" if eng.account_username else _esc(eng.account_name)
    vc = ("🟢 Voice chat ON hai — ready!" if call else
          "⚪ Voice chat abhi band hai — group me VC start karo (ya Sudo ID ko "
          "<b>Manage Video Chats</b> admin do, wo khud start kar legi).")
    note = "\n<i>Member check skip hua (privacy) — group me hona zaroori hai.</i>" if why == "unverified" else ""
    text = card("✅", "Group verified",
                f"🏷 <b>{_esc(chat.title)}</b>\n🆔 <code>{chat.id}</code>\n"
                f"🤖 Sudo ID: {who}\n{vc}{note}\n\n<b>Ab kya karna hai?</b>",
                "Audio: bot ko audio/voice bhejo aur us par reply me <code>.play</code> likho, "
                "ya <code>.play &lt;YouTube link&gt;</code>.")
    kb = K([[B("▶️ Audio Play", callback_data="sudo:play", style="success"),
             B("🎤 Live Mic", callback_data="sudo:mic", style="success")],
            [B("🔁 Group Badlo", callback_data="sudo:gc", style="primary"),
             B("🏠 Home", callback_data="menu:home", style="primary")]])
    return True, text, kb


async def sudo_setgc(msg: Message) -> None:
    """`.setgc <link>` for non-logged users (called from vc_commands.cmd_setgc)."""
    parts = cmd_text(msg).split()
    uid = msg.from_user.id
    ref = parts[1] if len(parts) > 1 else ""
    if not ref and msg.chat and msg.chat.id < 0:
        ref = f"@{msg.chat.username}" if msg.chat.username else str(msg.chat.id)
    if not ref:
        await _ask_gc(msg, uid)
        return
    wait = await msg.reply_text("🔎 Group verify ho raha hai…")
    ok, text, kb = await verify_gc(msg._client, msg.from_user, ref)
    await wait.edit_text(text, reply_markup=kb, disable_web_page_preview=True)


PLAY_HELP = card("▶️", "Audio kaise bajaye",
                 "① Bot ke DM me koi <b>audio / voice / video</b> bhejo\n"
                 "② Us par reply karke <code>.play</code> likho\n"
                 "   ya: <code>.play https://youtu.be/…</code>\n\n"
                 "Controls: <code>.pause</code> · <code>.resume</code> · <code>.skip</code> · <code>.stop</code>\n"
                 "Loud: <code>.max</code> · Volume: <code>.vol 200</code>",
                 "Har .play aapke verified group me hi bajega.")


# ══════════════════════════ USER CALLBACKS ════════════════════════════

@Client.on_callback_query(filters.regex(r"^sudo:"))
async def cb_sudo(bot: Client, cq):
    uid = cq.from_user.id
    action = cq.data.split(":", 1)[1]

    if action in ("home", "menu"):
        text, kb = await paywall_screen(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return await safe_answer(cq)

    if action == "trial":
        mode = await sudo.engine_mode(uid, fresh=True)
        if mode in (sudo.OWNER, sudo.SELF):
            await safe_answer(cq, "✅ Aap login ho — trial ki zarurat nahi", show_alert=True)
            return
        if mode == sudo.TRIAL_OVER:
            text, kb = await paywall_screen(uid)
            await edit_screen(cq.message, text, reply_markup=kb)
            return await safe_answer(cq, "Trial khatam ho chuka")
        if not await sudo.accounts():
            return await safe_answer(cq, "⚠️ Abhi Free Trial available nahi (owner ne Sudo ID add nahi ki). "
                                         "🔐 Apna Login use karo.", show_alert=True)
        if mode == sudo.NONE:
            st = await sudo.start_trial(uid)
            await safe_answer(cq, f"🎁 Trial start — {st['limit']} free uses", show_alert=False)
        else:
            await safe_answer(cq)
        await _ask_gc(cq.message, uid, edit=True)
        return

    if action == "gc":
        if await sudo.engine_mode(uid, fresh=True) not in (sudo.SUDO, sudo.PREMIUM, sudo.TRIAL):
            text, kb = await paywall_screen(uid)
            await edit_screen(cq.message, text, reply_markup=kb)
            return await safe_answer(cq, "Pehle Free Trial start karo")
        await _ask_gc(cq.message, uid, edit=True)
        return await safe_answer(cq)

    if action == "cancel":
        _await.pop(uid, None)
        text, kb = await paywall_screen(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return await safe_answer(cq, "Cancel")

    if action == "play":
        await edit_screen(cq.message, PLAY_HELP,
                          reply_markup=K([[B("🎤 Live Mic", callback_data="sudo:mic", style="success"),
                                           B("⬅ Back", callback_data="sudo:home", style="primary")]]))
        return await safe_answer(cq)

    if action == "mic":
        from helpers.feature_flags import refresh_flags
        if not await refresh_flags() and not Config.is_owner(uid):
            return await safe_answer(cq, "🎤 Live Mic abhi owner ne OFF kiya hai", show_alert=True)
        await safe_answer(cq, "🎤 Live Mic link ban raha hai…")
        from plugins.web_mic import run_web_mic
        m = SimpleNamespace(from_user=cq.from_user, chat=cq.message.chat, _client=bot)
        await run_web_mic(m, "on")
        return

    if action in ("req", "reqchk"):
        await _sudo_request(bot, cq)
        return
    await safe_answer(cq)


async def _sudo_request(bot: Client, cq):
    uid = cq.from_user.id
    mode = await sudo.engine_mode(uid, fresh=True)
    if mode in (sudo.SUDO, sudo.OWNER, sudo.SELF):
        return await safe_answer(cq, "✅ Aapke paas pehle se access hai", show_alert=True)
    block = await sudo.request_block_reason(uid)
    if block == "pending":
        return await safe_answer(cq, "⏳ Aapki request owner ke paas pending hai", show_alert=True)
    if block == "cooldown":
        return await safe_answer(cq, "⏳ Request reject hui thi — 1 ghante baad dobara try karo", show_alert=True)
    s = await sudo.settings()
    sup_url = sudo.support_url(s)
    sid = int(s.get("support_id") or 0)
    if sid:
        joined = False
        try:
            from pyrogram.enums import ChatMemberStatus as S
            m = await bot.get_chat_member(sid, uid)
            joined = m.status not in (S.LEFT, S.BANNED)
        except Exception:
            joined = False
        if not joined:
            rows = []
            if sup_url:
                rows.append([B("🆘 Support GC Join karo", url=sup_url)])
            rows.append([B("✅ Join kar liya — Check", callback_data="sudo:reqchk", style="success")])
            rows.append([B("⬅ Back", callback_data="sudo:home", style="primary")])
            await edit_screen(cq.message, card("🆘", "Pehle Support GC join karo",
                              "Sudo sirf Support GC members ko milti hai.\n"
                              "① Join button dabao\n② Wapas aa kar <b>Check</b> dabao"),
                              reply_markup=K(rows))
            return await safe_answer(cq, "Pehle Support GC join karo" if cq.data.endswith("reqchk") else "")
    u = cq.from_user
    refs, need = await _ref_progress(uid)
    t = await sudo.trial_state(uid)
    await sudo.set_request(uid, "pending", name=u.first_name or "", username=u.username or "")
    text = (f"🙋 <b>Sudo Request</b>\n{LINE}\n"
            f"👤 {_esc(u.first_name)} {('@' + u.username) if u.username else ''}\n"
            f"🆔 <code>{uid}</code>\n🎁 Trial: {t['used']}/{t['limit']}\n👥 Referrals: {refs}/{need}")
    kb = K([[B("✅ 1 Din", callback_data=f"sadm:ok:{uid}:1", style="success"),
             B("✅ 7 Din", callback_data=f"sadm:ok:{uid}:7", style="success")],
            [B("✅ 30 Din", callback_data=f"sadm:ok:{uid}:30", style="success"),
             B("♾ Permanent", callback_data=f"sadm:ok:{uid}:0", style="success")],
            [B("❌ Reject", callback_data=f"sadm:no:{uid}", style="danger")]])
    sent = False
    for oid in Config.OWNER_IDS:
        try:
            await bot.send_message(oid, text, reply_markup=kb)
            sent = True
        except Exception:
            continue
    if not sent:
        await sudo.set_request(uid, "none")
        return await safe_answer(cq, "Owner tak request nahi pahunchi — Support GC me message karo", show_alert=True)
    await edit_screen(cq.message, card("📨", "Request bhej di",
                      "Owner ko aapki Sudo request mil gayi. Approve hote hi yahin message aayega."),
                      reply_markup=K([[B("🏠 Home", callback_data="menu:home", style="primary")]]))
    await safe_answer(cq, "✅ Request sent")


# ══════════════════════════ TEXT INPUT (users + owner) ═══════════════

def _in_login(uid: int) -> bool:
    try:
        from plugins.login import CONVERSATION
        return int(uid) in CONVERSATION
    except Exception:
        return False


def clear_await(uid: int) -> None:
    """Login shuru hote hi pending 'group link bhejo' prompt hata do."""
    _await.pop(int(uid), None)


def _awaiting(_, __, m):
    return bool(m.from_user and _get_await(m.from_user.id))


@Client.on_message(HAS_USER & filters.private & filters.text & filters.create(_awaiting)
                   & ~filters.regex(r"^[/.!]"), group=1)
async def sudo_text_listener(bot: Client, msg: Message):
    uid = msg.from_user.id
    st = _get_await(uid)
    if not st:
        return
    kind = st["kind"]
    text = (msg.text or "").strip()

    if kind == "gc":
        # ROOT FIX (login kharab): Free Trial dabane ke baad user Login karta
        # to uska phone number / OTP yahi "group link" samajh kar kha liya
        # jata tha (stop_propagation) -> login kabhi complete nahi hota tha.
        if _in_login(uid) or not _looks_like_group_ref(text.split()[0] if text else ""):
            if _in_login(uid) or _PHONE_RE.match(text):
                _await.pop(uid, None)
                return          # login.py ko message milne do
            await msg.reply_text("❌ Ye group link nahi lagta. <code>https://t.me/…</code> ya "
                                 "<code>@groupname</code> bhejo — ya ✖ Cancel dabao.",
                                 reply_markup=K([[B("✖ Cancel", callback_data="sudo:cancel",
                                                    style="danger")]]))
            msg.stop_propagation()
        _await.pop(uid, None)
        wait = await msg.reply_text("🔎 Sudo ID group join karke verify kar rahi hai…")
        ok, out, kb = await verify_gc(bot, msg.from_user, text.split()[0] if text else "")
        await wait.edit_text(out, reply_markup=kb, disable_web_page_preview=True)
        if not ok:
            _set_await(uid, "gc")
        msg.stop_propagation()

    if not Config.is_owner(uid):
        return
    try:
        await msg.delete()  # string / OTP / password chat me na rahe
    except Exception:
        pass
    if kind == "str":
        _await.pop(uid, None)
        reply = await _owner_add_string(text)
    elif kind == "phone":
        reply = await _owner_phone(uid, text)
    elif kind in ("code", "pass"):
        reply = await _owner_code(uid, text, kind)
    elif kind == "support":
        _await.pop(uid, None)
        reply = await _owner_set_support(bot, text)
    else:
        return
    await bot.send_message(msg.chat.id, reply,
                           reply_markup=K([[B("👑 Sudo Panel", callback_data="sadm:home", style="primary")]]),
                           disable_web_page_preview=True)
    msg.stop_propagation()


# ══════════════════════════ OWNER PANEL ═══════════════════════════════

async def _owner_add_string(session: str) -> str:
    from pyrogram import Client as _C
    session = (session or "").strip()
    if len(session) < 100 or " " in session:
        return "❌ Valid string session nahi lagta. Poora Pyrogram string paste karein."
    c = _C(name="sudo_check", api_id=Config.API_ID, api_hash=Config.API_HASH,
           session_string=session, in_memory=True, no_updates=True)
    try:
        await c.connect()
        me = await c.get_me()
    except Exception as e:
        return f"❌ Session kaam nahi kar raha: <code>{_esc(e)}</code>"
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass
    ok, how = await sudo.add_account(session, {"id": me.id, "name": me.first_name or "",
                                               "username": me.username or ""})
    return (f"✅ Sudo ID {'update' if how == 'updated' else 'add'} ho gayi: "
            f"<b>{_esc(me.first_name)}</b> {('@' + me.username) if me.username else ''} "
            f"(<code>{me.id}</code>)") if ok else f"❌ {how}"


async def _login_cancel(uid: int) -> None:
    st = _logins.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass


async def _owner_phone(uid: int, text: str) -> str:
    from pyrogram import Client as _C
    if len(text) > 100 and " " not in text:
        _await.pop(uid, None)
        return await _owner_add_string(text)
    phone = re.sub(r"[\s\-()]", "", text)
    if not re.fullmatch(r"\+?\d{10,15}", phone):
        return "❌ Number country code ke saath bhejo, jaise <code>+919876543210</code>."
    phone = phone if phone.startswith("+") else "+" + phone
    await _login_cancel(uid)
    c = _C(name=f"sudologin_{uid}", api_id=Config.API_ID, api_hash=Config.API_HASH,
           in_memory=True, no_updates=True)
    try:
        await c.connect()
        sent = await c.send_code(phone)
    except Exception as e:
        try:
            await c.disconnect()
        except Exception:
            pass
        return f"❌ OTP nahi gaya: <code>{_esc(e)}</code>"
    _logins[uid] = {"client": c, "phone": phone, "hash": sent.phone_code_hash}
    _set_await(uid, "code")
    return "📩 OTP bhej diya. Ab OTP yahin bhejo — <b>space ke saath</b> jaise <code>1 2 3 4 5</code>."


async def _owner_code(uid: int, text: str, kind: str) -> str:
    from pyrogram.errors import (PasswordHashInvalid, PhoneCodeExpired, PhoneCodeInvalid,
                                 SessionPasswordNeeded)
    st = _logins.get(uid)
    if not st:
        _await.pop(uid, None)
        return "❌ Login session expire. Dobara 📱 Phone se add karo."
    c = st["client"]
    try:
        if kind == "code":
            code = re.sub(r"\D", "", text)
            try:
                await c.sign_in(st["phone"], st["hash"], code)
            except SessionPasswordNeeded:
                _set_await(uid, "pass")
                return "🔐 2-Step Verification ON hai — password bhejo."
        else:
            await c.check_password(text)
        session = await c.export_session_string()
    except (PhoneCodeInvalid, PhoneCodeExpired):
        return "❌ OTP galat/expire. Sahi OTP bhejo ya Cancel karke dobara."
    except PasswordHashInvalid:
        return "❌ Password galat. Dobara bhejo."
    except Exception as e:
        _await.pop(uid, None)
        await _login_cancel(uid)
        return f"❌ Login fail: <code>{_esc(e)}</code>"
    _await.pop(uid, None)
    await _login_cancel(uid)
    return await _owner_add_string(session)


async def _owner_set_support(bot, text: str) -> str:
    parts = text.split()
    if not parts:
        return "❌ Kuch bheja nahi."
    if parts[0].lower() in ("off", "none", "clear"):
        await sudo.update_settings(support="", support_id=0)
        return "✅ Support GC hata diya."
    link = next((p for p in parts if "t.me/" in p or p.startswith("@")), "")
    num = next((p for p in parts if re.fullmatch(r"-100\d{6,}", p)), "")
    sid = int(num) if num else 0
    if not sid and link:
        key = link[1:] if link.startswith("@") else link
        m = _PUBLIC_RE.search(link)
        if m:
            key = m.group(1)
        try:
            chat = await bot.get_chat(key)
            sid = int(chat.id)
            if not link.startswith("http") and not link.startswith("t.me"):
                link = f"@{getattr(chat, 'username', '') or key}"
        except Exception:
            sid = 0
    if not link and sid:
        try:
            link = (await bot.export_chat_invite_link(sid)) or ""
        except Exception:
            link = ""
    await sudo.update_settings(support=link, support_id=sid)
    check = ("✅ Join-check ON (bot group me hai)" if sid else
             "⚠️ Join-check OFF — bot ko support GC me add karke link ke saath numeric ID bhi bhejo")
    return f"✅ Support GC set: {_esc(link) or sid}\n{check}"


async def _panel(uid: int) -> tuple[str, K]:
    s = await sudo.settings()
    rows = await sudo.accounts()
    users = await sudo.sudo_users()
    live = sudo.live_engines()
    acc_lines = []
    for r in rows[:10]:
        eng = live.get(r["key"])
        dot = "🟢" if eng else "⚪"
        chats = len(sudo.busy_chats(r["key"]))
        nm = r.get("name") or "ID"
        un = f"@{r['username']}" if r.get("username") else ""
        acc_lines.append(f"{dot} {_esc(nm)} {un} · {'🔴 busy' if chats else '🟢 free'}")
    body = (f"🤖 <b>Sudo IDs:</b> {len(rows)}\n" + ("\n".join(acc_lines) or "— koi nahi (pehle add karo)") +
            f"\n\n👑 <b>Sudo users:</b> {len(users)}\n"
            f"🎁 <b>Trial uses:</b> {s['trial_uses']} (bina login)\n"
            f"👥 <b>Referral → Premium:</b> {s['ref_needed']} referrals\n"
            f"🆘 <b>Support GC:</b> {_esc(s['support']) or '— set nahi'}"
            f"{' ✅' if s.get('support_id') else ''}")
    kb = K([
        [B("➕ String se Sudo ID", callback_data="sadm:addstr", style="success"),
         B("📱 Phone/OTP se", callback_data="sadm:addphone", style="success")],
        [B("🤖 Sudo IDs", callback_data="sadm:ids", style="primary"),
         B("👑 Sudo Users", callback_data="sadm:users", style="primary")],
        [B("🎁 Trial −1", callback_data="sadm:trial:-1", style="primary"),
         B(f"{s['trial_uses']}", callback_data="sadm:noop", style="primary"),
         B("🎁 Trial +1", callback_data="sadm:trial:1", style="primary")],
        [B("🆘 Support GC Set", callback_data="sadm:support", style="primary")],
        [B("⬅ Owner Panel", callback_data="adm_back", style="primary")],
    ])
    return card("👑", "Sudo panel", body,
                "Users bina login inhi Sudo IDs se VC chalate hain — 1 Sudo ID = 1 group ek time par, "
                "zyada users ke liye zyada IDs add karo. "
                "Commands: /addsudo id [days] · /rmsudo id · /sudolist · /resettrial id"), kb


@Client.on_callback_query(filters.regex(r"^sadm:"))
async def cb_sudo_admin(bot: Client, cq):
    uid = cq.from_user.id
    if not Config.is_owner(uid):
        return await safe_answer(cq, "Sirf owner!", show_alert=True)
    parts = cq.data.split(":")
    action = parts[1]
    back = K([[B("⬅ Sudo Panel", callback_data="sadm:home", style="primary")]])

    if action == "noop":
        return await safe_answer(cq)
    if action == "home":
        _await.pop(uid, None)
        await _login_cancel(uid)
        text, kb = await _panel(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return await safe_answer(cq)
    if action == "addstr":
        _set_await(uid, "str")
        await edit_screen(cq.message, card("➕", "Sudo ID — String",
                          "Pyrogram <b>string session</b> yahin paste karo.\n"
                          f"String banane ke liye: {_esc(Config.SESSION_BOT_LINK)}",
                          "Message turant delete ho jayega."), reply_markup=back)
        return await safe_answer(cq)
    if action == "addphone":
        _set_await(uid, "phone")
        await edit_screen(cq.message, card("📱", "Sudo ID — Phone login",
                          "Sudo ID ka number bhejo (country code ke saath)\n"
                          "jaise <code>+919876543210</code>. Phir OTP aur (agar ho) 2FA password.",
                          "Sab messages auto-delete honge."), reply_markup=back)
        return await safe_answer(cq)
    if action == "support":
        _set_await(uid, "support")
        await edit_screen(cq.message, card("🆘", "Support GC",
                          "Support group ka <code>@username</code> / link bhejo.\n"
                          "Private group: <code>https://t.me/+xxxx -1001234567890</code> (link + ID)\n"
                          "Hatane ke liye: <code>off</code>",
                          "Join-check ke liye bot ko support group me add karo."), reply_markup=back)
        return await safe_answer(cq)
    if action == "trial":
        s = await sudo.settings()
        await sudo.update_settings(trial_uses=int(s["trial_uses"]) + int(parts[2]))
        sudo._cache_clear()
        text, kb = await _panel(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return await safe_answer(cq)
    if action == "ids":
        rows = await sudo.accounts()
        btns = [[B(f"❌ {r.get('name') or 'ID'} {('@' + r['username']) if r.get('username') else ''}",
                   callback_data=f"sadm:rm:{r['key']}", style="danger")] for r in rows[:20]]
        btns.append([B("⬅ Sudo Panel", callback_data="sadm:home", style="primary")])
        await edit_screen(cq.message, card("🤖", "Sudo IDs",
                          "Hatane ke liye ID par tap karo." if rows else "Koi Sudo ID nahi."),
                          reply_markup=K(btns))
        return await safe_answer(cq)
    if action == "rm":
        ok = await sudo.remove_account(parts[2])
        await safe_answer(cq, "✅ Hata di" if ok else "Nahi mili")
        text, kb = await _panel(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return
    if action == "users":
        users = await sudo.sudo_users()
        lines = [f"• <code>{u}</code> · {format_until(until)}" for u, until in users[:40]]
        btns = [[B(f"❌ {u}", callback_data=f"sadm:unsudo:{u}", style="danger")] for u, _ in users[:15]]
        btns.append([B("⬅ Sudo Panel", callback_data="sadm:home", style="primary")])
        await edit_screen(cq.message, card("👑", "Sudo users",
                          "\n".join(lines) or "Koi sudo user nahi.",
                          "Add: /addsudo user_id [days]  (days na do = permanent)"),
                          reply_markup=K(btns))
        return await safe_answer(cq)
    if action == "unsudo":
        await sudo.revoke_sudo(int(parts[2]))
        await safe_answer(cq, "✅ Sudo hata diya")
        cq.data = "sadm:users"
        return await cb_sudo_admin(bot, cq)
    if action == "ok":
        target, days = int(parts[2]), int(parts[3])
        until = await sudo.grant_sudo(target, days, by=uid)
        await sudo.set_request(target, "approved", by=uid)
        try:
            await bot.send_message(target, card("👑", "Sudo mil gaya!",
                                   f"Owner ne aapko Sudo diya · till <b>{format_until(until)}</b>\n"
                                   "Ab bina login unlimited .play + Live Mic.\n/start dabao."),
                                   reply_markup=K([[B("🎯 Group Set Karo", callback_data="sudo:gc", style="success")]]))
        except Exception:
            pass
        await edit_screen(cq.message, (cq.message.text or cq.message.caption or "").split("\n")[0] +
                          f"\n\n✅ Approved by <code>{uid}</code> · till {format_until(until)}")
        return await safe_answer(cq, "✅ Approved")
    if action == "no":
        target = int(parts[2])
        await sudo.set_request(target, "rejected", by=uid)
        try:
            await bot.send_message(target, "❌ Aapki Sudo request reject ho gayi. "
                                           "Apna 🔐 Login karo ya 5 referrals se Premium lo.")
        except Exception:
            pass
        await edit_screen(cq.message, (cq.message.text or "").split("\n")[0] + "\n\n❌ Rejected")
        return await safe_answer(cq, "Rejected")
    await safe_answer(cq)


# ══════════════════════════ OWNER COMMANDS ════════════════════════════

def _owner(msg) -> bool:
    return bool(msg.from_user and Config.is_owner(msg.from_user.id))


@Client.on_message(HAS_USER & dm_cmd(["sudo", "sudopanel"]) & filters.private)
async def cmd_sudo_panel(bot: Client, msg: Message):
    if not _owner(msg):
        text, kb = await paywall_screen(msg.from_user.id)
        return await msg.reply_text(text, reply_markup=kb)
    text, kb = await _panel(msg.from_user.id)
    await msg.reply_text(text, reply_markup=kb)


async def _target_uid(msg, parts) -> int:
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    if len(parts) > 1:
        ref = parts[1]
        if re.fullmatch(r"\d{4,}", ref):
            return int(ref)
        try:
            return (await msg._client.get_users(ref.lstrip("@"))).id
        except Exception:
            return 0
    return 0


@Client.on_message(HAS_USER & dm_cmd("addsudo"))
async def cmd_addsudo(bot: Client, msg: Message):
    if not _owner(msg):
        return
    parts = cmd_text(msg).split()
    target = await _target_uid(msg, parts)
    if not target:
        return await msg.reply_text("Use: <code>/addsudo user_id [days]</code> — days na do = permanent")
    days = 0
    nums = [p for p in parts[2:] if p.isdigit()]
    if nums:
        days = int(nums[0])
    until = await sudo.grant_sudo(target, days, by=msg.from_user.id)
    await msg.reply_text(f"✅ <code>{target}</code> ko Sudo diya · till {format_until(until)}")
    try:
        await bot.send_message(target, f"👑 <b>Aapko Sudo mil gaya!</b> · till {format_until(until)}\n"
                                       "Bina login bot use karo — /start dabao.")
    except Exception:
        pass


@Client.on_message(HAS_USER & dm_cmd(["rmsudo", "delsudo"]))
async def cmd_rmsudo(bot: Client, msg: Message):
    if not _owner(msg):
        return
    target = await _target_uid(msg, cmd_text(msg).split())
    if not target:
        return await msg.reply_text("Use: <code>/rmsudo user_id</code>")
    ok = await sudo.revoke_sudo(target)
    await msg.reply_text("✅ Sudo hata diya." if ok else "Is user ke paas sudo nahi tha.")


@Client.on_message(HAS_USER & dm_cmd("sudolist") & filters.private)
async def cmd_sudolist(bot: Client, msg: Message):
    if not _owner(msg):
        return
    users = await sudo.sudo_users()
    lines = [f"• <code>{u}</code> · {format_until(until)}" for u, until in users]
    await msg.reply_text("👑 <b>Sudo users</b>\n" + ("\n".join(lines) or "— koi nahi"))


@Client.on_message(HAS_USER & dm_cmd("resettrial") & filters.private)
async def cmd_resettrial(bot: Client, msg: Message):
    if not _owner(msg):
        return
    target = await _target_uid(msg, cmd_text(msg).split())
    if not target:
        return await msg.reply_text("Use: <code>/resettrial user_id</code>")
    await sudo.reset_trial(target)
    await msg.reply_text(f"✅ <code>{target}</code> ka trial reset — dobara free uses milenge.")
