"""Payment system: owner price/QR/coupon management + user plan display & requests.

Owner commands:
  .setprice <plan_id> <price>  — set plan price
  .grant <user_id> <plan_id>   — manually grant premium
  .revoke <user_id>            — revoke premium
  .addcoupon <code> <plan_id> [max_uses]  — create coupon
  .delcoupon <code>            — delete coupon
  .listcoupons                 — list all coupons
  .redeemlogs [code]           — kisne kaunsa coupon redeem kiya
  .setqr (reply to photo)      — set QR code image
  .clearqr                     — remove QR code
  .pending                     — list pending payment requests

User commands:
  .redeem <code>               — redeem coupon code
  .plans                       — view plans & buy

Callbacks:
  pay:menu / pay:plan:<id> / pay:request:<id>
  pay:approve:<id> / pay:reject:<id>  (owner only)
  pay:coupon / pay:owner_panel
"""


import html
import json
import secrets
import time

from pyrogram import Client, filters
from plugins.ui import dm_cmd
from pyrogram.types import InlineKeyboardMarkup as K, Message

from config import Config
from helpers.access_control import (
    PAYMENT_CONTACT_URL, PAYMENT_PLANS,
    get_payment_config, get_plan_price, get_qr_code, grant_premium,
    format_until, plan_label, premium_info, redeem_coupon, revoke_premium_all,
    set_payment_config, set_qr_code, time_left,
)
from helpers.database import db
from plugins.ui import HAS_USER, cmd_text, B, LINE, card, edit_screen, safe_answer

_VALID_PLANS = {p[0] for p in PAYMENT_PLANS}
_esc = html.escape

# Track users who are expected to send a payment screenshot
# Persisted in DB so it survives restarts.
SS_PENDING_KEY = "pending_payment_ss"


async def _load_pending_ss() -> dict:
    """Keys are ALWAYS str(user_id). JSON turns int keys into strings, so the
    old `user_id in pending` (int) never matched after the first save and
    payment screenshots were silently ignored."""
    raw = await db.get_app_value(SS_PENDING_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()}
        except (ValueError, TypeError):
            pass
    return {}


async def _save_pending_ss(data: dict):
    await db.set_app_value(SS_PENDING_KEY, json.dumps({str(k): v for k, v in data.items()}))


# Users who tapped "🎟️ Redeem Coupon" and whose next text is the code.
_AWAIT_CODE: dict = {}
_AWAIT_TTL = 300


def _awaiting(uid: int) -> bool:
    t = _AWAIT_CODE.get(uid)
    if t and time.time() - t < _AWAIT_TTL:
        return True
    _AWAIT_CODE.pop(uid, None)
    return False


# ── Owner: price & coupon panel ──────────────────────────

def _payment_panel_text() -> str:
    return card("💳", "Payment management",
        "<b>Prices & premium</b>\n"
        "• <code>.setprice &lt;plan_id&gt; &lt;price&gt;</code>\n"
        "• <code>.grant &lt;user_id&gt; &lt;plan_id&gt;</code>\n"
        "• <code>.revoke &lt;user_id&gt;</code>\n\n"
        "<b>Coupons</b>\n"
        "• <code>.addcoupon &lt;code|auto&gt; &lt;plan_id&gt; [max_uses]</code>\n"
        "• <code>.delcoupon &lt;code&gt;</code> · <code>.listcoupons</code>\n"
        "• <code>.redeemlogs [code]</code>\n\n"
        "<b>QR & requests</b>\n"
        "• <code>.setqr</code> (QR photo pe reply) · <code>.clearqr</code>\n"
        "• <code>.pending</code> — pending requests\n\n"
        "<b>Plans:</b> " + " · ".join(f"<code>{p[0]}</code>" for p in PAYMENT_PLANS),
        "Har coupon ek user sirf 1 baar use kar sakta hai. max_uses = kitne alag users.")


def payment_owner_kb() -> K:
    return K([
        [B("🔄 Refresh", callback_data="pay:refresh"),
         B("📋 Pending Requests", callback_data="pay:pending")],
        [B("⬅ Owner Panel", callback_data="adm_back")],
    ])


async def _prices_text() -> str:
    config = await get_payment_config()
    lines = []
    for plan_id, label, _, _ in PAYMENT_PLANS:
        price = config.get(plan_id, {}).get("price") or "— not set —"
        lines.append(f"• <b>{label}</b> (<code>{plan_id}</code>) → <code>{_esc(str(price))}</code>")
    return card("💳", "Plans & prices", "\n".join(lines))


# ── User: plans display ──────────────────────────────────

def _premium_status_block(pinfo: dict) -> str:
    if pinfo.get("active"):
        if pinfo.get("plan") == "owner":
            return "👑 <b>Owner</b> · unlimited access"
        left = time_left(pinfo.get("until"))
        return (f"💎 <b>Premium active</b> · {plan_label(pinfo.get('plan'))}\n"
                f"📅 Valid till <b>{format_until(pinfo.get('until'))}</b>"
                + (f"\n⏳ {left} baaki" if left and left != "expired" else ""))
    return "🆓 <b>Free plan</b> · roz ki limit lagi hai"


async def _user_plans_text(user_id: int) -> str:
    config = await get_payment_config()
    pinfo = await premium_info(user_id)
    lines = []
    for plan_id, label, _, _ in PAYMENT_PLANS:
        entry = config.get(plan_id) or {}
        price = entry.get("price") or "—"
        lines.append(f"🔹 <b>{label}</b>  ·  <code>{_esc(str(price))}</code>")
    return card("💎", "Premium",
        f"<blockquote>{_premium_status_block(pinfo)}</blockquote>\n"
        "<b>Plans</b>\n" + "\n".join(lines) + "\n\n"
        "♾️ Premium = <b>unlimited</b> uses, koi daily limit nahi.",
        "Plan chuno → QR pe pay karo → screenshot bhejo → owner approve karega.\n"
        "Coupon hai? 🎟️ <b>Redeem Coupon</b> dabao aur code bhejo.")


def user_payment_kb(config: dict = None) -> K:
    config = config or {}
    buttons = []
    for plan_id, label, _, _ in PAYMENT_PLANS:
        entry = config.get(plan_id) or {}
        price = entry.get("price")
        text = f"{label} · {price}" if price else label
        buttons.append(B(f"💠 {text}", callback_data=f"pay:plan:{plan_id}", style="primary"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([B("🎟️ Redeem Coupon", callback_data="pay:coupon", style="success")])
    rows.append([B("🏠 Home", callback_data="menu:home", style="primary")])
    return K(rows)


def _redeem_prompt_text() -> str:
    return card("🎟️", "Redeem coupon",
        "Apna coupon code <b>abhi yahan bhejo</b> 👇\n"
        "<i>Example:</i> <code>APEX7DAY</code>",
        "Ek code ek user sirf 1 baar use kar sakta hai. "
        "Command se bhi kar sakte ho: <code>.redeem CODE</code>")


def _redeem_prompt_kb() -> K:
    return K([[B("✖️ Cancel", callback_data="pay:coupon_cancel", style="danger"),
               B("💎 Plans", callback_data="pay:menu", style="primary")]])


def _redeem_result(result: dict):
    if result.get("ok"):
        until = result.get("until")
        left = time_left(until)
        text = card("🎉", "Premium activated",
            f"<blockquote>💎 <b>{plan_label(result.get('plan'))}</b> plan unlock ho gaya!</blockquote>\n"
            f"📅 Valid till: <b>{format_until(until)}</b>\n"
            + (f"⏳ Time left: <b>{left}</b>\n" if left and left != "expired" else "")
            + "♾️ Ab koi daily limit nahi — enjoy!")
        kb = K([[B("💎 My Plan", callback_data="pay:menu", style="success"),
                 B("🏠 Home", callback_data="menu:home", style="primary")]])
        return text, kb
    icon = {"already": "♻️", "exhausted": "⌛", "rate_limited": "⏱️",
            "error": "⚠️"}.get(result.get("reason"), "❌")
    text = card(icon, "Redeem failed", _esc(result.get("message") or "Redeem nahi hua."))
    rows = []
    if result.get("reason") not in ("rate_limited",):
        rows.append([B("🔁 Try another code", callback_data="pay:coupon", style="primary")])
    rows.append([B("💎 Plans", callback_data="pay:menu", style="primary"),
                 B("🏠 Home", callback_data="menu:home", style="primary")])
    return text, K(rows)


async def _do_redeem(msg: Message, code: str):
    u = msg.from_user
    _AWAIT_CODE.pop(u.id, None)
    result = await redeem_coupon(u.id, code, u.username or "",
                                 " ".join(x for x in (u.first_name, u.last_name) if x))
    text, kb = _redeem_result(result)
    await msg.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
    if result.get("ok"):
        try:
            from helpers.logger_channel import get_channel
            cid = get_channel()
            if cid:
                who = f"@{u.username}" if u.username else _esc(u.first_name or "-")
                await msg._client.send_message(
                    cid, f"🎟️ <b>Coupon redeemed</b>\n"
                         f"Code: <code>{_esc(code.strip().upper())}</code> · {plan_label(result.get('plan'))}\n"
                         f"User: {who} (<code>{u.id}</code>)")
        except Exception:
            pass


# ── Owner commands ───────────────────────────────────────

@Client.on_message(HAS_USER & dm_cmd("setprice") & filters.private)
async def cmd_setprice(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split(maxsplit=2)
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: <code>.setprice &lt;plan_id&gt; &lt;price&gt;</code>\n\n"
            "Plans: " + ", ".join(p[0] for p in PAYMENT_PLANS)
        )
        return
    plan_id = parts[1].strip().lower()
    price = parts[2].strip()
    valid_ids = {p[0] for p in PAYMENT_PLANS}
    if plan_id not in valid_ids:
        await msg.reply_text(f"❌ Invalid plan. Valid: {', '.join(sorted(valid_ids))}")
        return
    config = await get_payment_config()
    config[plan_id] = {"price": price}
    await set_payment_config(config)
    label = next(p[1] for p in PAYMENT_PLANS if p[0] == plan_id)
    await msg.reply_text(f"✅ <b>{label}</b> price set: <code>{price}</code>")


@Client.on_message(HAS_USER & dm_cmd("grant") & filters.private)
async def cmd_grant(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split(maxsplit=2)
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: <code>.grant &lt;user_id&gt; &lt;plan_id&gt;</code>\n"
            "Plans: " + ", ".join(p[0] for p in PAYMENT_PLANS)
        )
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await msg.reply_text("❌ User ID number hona chahiye.")
        return
    plan_id = parts[2].strip().lower()
    valid_ids = {p[0] for p in PAYMENT_PLANS}
    if plan_id not in valid_ids:
        await msg.reply_text(f"❌ Invalid plan. Valid: {', '.join(sorted(valid_ids))}")
        return
    try:
        until_iso = await grant_premium(uid, plan_id)
    except ValueError as exc:
        await msg.reply_text(f"❌ {exc}")
        return
    label = next(p[1] for p in PAYMENT_PLANS if p[0] == plan_id)
    await msg.reply_text(
        f"✅ Premium granted!\nUser: <code>{uid}</code>\n"
        f"Plan: <b>{label}</b>\nUntil: <code>{until_iso}</code>"
    )
    try:
        await bot.send_message(
            uid,
            f"🎉 <b>Premium Activated!</b>\n"
            f"Plan: <b>{label}</b>\nUntil: <code>{until_iso}</code>\n\n"
            "Ab aap unlimited uses kar sakte hain!"
        )
    except Exception:
        pass


@Client.on_message(HAS_USER & dm_cmd("revoke") & filters.private)
async def cmd_revoke(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply_text("Usage: <code>.revoke &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    ok = await revoke_premium_all(uid)
    await msg.reply_text(
        f"✅ Premium revoked for <code>{uid}</code>" if ok
        else f"❌ User <code>{uid}</code> ke paas premium nahi tha."
    )


@Client.on_message(HAS_USER & dm_cmd("addcoupon") & filters.private)
async def cmd_addcoupon(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split()
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: <code>.addcoupon &lt;code|auto&gt; &lt;plan_id&gt; [max_uses]</code>\n"
            "Example: <code>.addcoupon auto 7day 10</code>\n"
            "Plans: " + ", ".join(p[0] for p in PAYMENT_PLANS)
        )
        return
    code = parts[1].strip().upper()
    if code == "AUTO":
        code = "APEX-" + secrets.token_hex(4).upper()
    if not code.replace("-", "").replace("_", "").isalnum() or len(code) > 32:
        await msg.reply_text("❌ Code me sirf A-Z, 0-9, - aur _ (max 32) chalenge.")
        return
    plan_id = parts[2].strip().lower()
    if plan_id not in _VALID_PLANS:
        await msg.reply_text(f"❌ Invalid plan. Valid: {', '.join(sorted(_VALID_PLANS))}")
        return
    max_uses = 1
    if len(parts) > 3:
        if not parts[3].isdigit() or not (1 <= int(parts[3]) <= 100000):
            await msg.reply_text("❌ max_uses 1 se 100000 ke beech number hona chahiye.")
            return
        max_uses = int(parts[3])
    ok = await db.create_coupon(code, plan_id, max_uses, msg.from_user.id)
    if ok:
        await msg.reply_text(card("🎟️", "Coupon created",
            f"Code: <code>{code}</code>\n"
            f"Plan: <b>{plan_label(plan_id)}</b>\n"
            f"Uses: <b>{max_uses}</b> alag users (har user sirf 1 baar)",
            f"User bheje: <code>.redeem {code}</code>"))
    elif ok is None:
        await msg.reply_text("⚠️ Database abhi busy hai, coupon nahi bana. 1 min baad try karein.")
    else:
        await msg.reply_text(f"❌ Coupon <code>{code}</code> already exists.")


@Client.on_message(HAS_USER & dm_cmd("delcoupon") & filters.private)
async def cmd_delcoupon(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.delcoupon &lt;code&gt;</code>")
        return
    code = parts[1].strip().upper()
    ok = await db.delete_coupon(code)
    await msg.reply_text(
        f"✅ Coupon deleted: <code>{_esc(code)}</code>" if ok
        else "❌ Coupon nahi mila."
    )


@Client.on_message(HAS_USER & dm_cmd("listcoupons") & filters.private)
async def cmd_listcoupons(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    coupons = await db.all_coupons()
    if not coupons:
        await msg.reply_text("📋 Koi coupon nahi hai. Banane ke liye: <code>.addcoupon auto 7day 10</code>")
        return
    lines = []
    for c in coupons[:80]:
        used = int(c.get("used_count", 0) or 0)
        mx = int(c.get("max_uses", 1) or 1)
        icon = "🔴" if used >= mx else "🟢"
        lines.append(f"{icon} <code>{_esc(str(c['code']))}</code> · {plan_label(c.get('plan_id'))} "
                     f"· <b>{used}/{mx}</b> used")
    more = f"\n<i>+{len(coupons) - 80} aur…</i>" if len(coupons) > 80 else ""
    await msg.reply_text(card("🎟️", f"All coupons ({len(coupons)})",
                              "\n".join(lines) + more,
                              "🟢 active · 🔴 khatam. Details: <code>.redeemlogs CODE</code>"))


@Client.on_message(HAS_USER & dm_cmd("redeemlogs") & filters.private)
async def cmd_redeemlogs(bot: Client, msg: Message):
    """Owner: kis user ne kaunsa coupon redeem kiya. .redeemlogs [code]"""
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split(maxsplit=1)
    logs = await db.redeem_logs(parts[1] if len(parts) > 1 else None)
    if not logs:
        await msg.reply_text("📋 Abhi tak koi redeem log nahi hai.")
        return
    lines = []
    for r in logs:
        who = f"@{_esc(r['username'])}" if r.get("username") else _esc(r.get("name") or "-")
        lines.append(f"• <code>{_esc(str(r.get('code')))}</code> · {plan_label(r.get('plan_id'))}\n"
                     f"   👤 {who} (<code>{r.get('user_id')}</code>) · 🕒 {format_until(r.get('redeemed_at'))}")
    await msg.reply_text(card("🎟️", f"Redeem logs ({len(logs)})", "\n".join(lines)))


@Client.on_message(HAS_USER & dm_cmd("setqr") & filters.private)
async def cmd_setqr(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    reply = msg.reply_to_message
    if not reply or not (reply.photo or (reply.document and reply.document.mime_type and "image" in reply.document.mime_type)):
        await msg.reply_text(
            "❌ <b>QR code set karne ka tareeka:</b>\n\n"
            "1. QR code ki photo bot ko bhejein\n"
            "2. Us photo ko reply karke <code>.setqr</code> likhein\n\n"
            "Ya agar photo already bheji hai, to us par reply karke command do."
        )
        return
    photo = reply.photo
    file_id = photo.file_id if photo else reply.document.file_id
    await set_qr_code(file_id)
    await msg.reply_text("✅ QR code set ho gaya. Ab users plan select karke request karte hi QR DM mein mil jayega.")


@Client.on_message(HAS_USER & dm_cmd("clearqr") & filters.private)
async def cmd_clearqr(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    await set_qr_code("")
    await msg.reply_text("✅ QR code removed.")


@Client.on_message(HAS_USER & dm_cmd("pending") & filters.private)
async def cmd_pending(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    requests = await db.pending_payment_requests()
    if not requests:
        await msg.reply_text("📋 Koi pending payment request nahi hai.")
        return
    lines = []
    for r in requests:
        lines.append(
            f"• <code>{r['request_id']}</code> — User: <code>{r['user_id']}</code> "
            f"Plan: <code>{r['plan_id']}</code>"
        )
    await msg.reply_text(
        "📋 <b>Pending Payment Requests</b>\n" + "\n".join(lines)
    )


# ── User: redeem coupon ──────────────────────────────────

@Client.on_message(HAS_USER & dm_cmd("redeem") & filters.private)
async def cmd_redeem(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        _AWAIT_CODE[msg.from_user.id] = time.time()
        await msg.reply_text(_redeem_prompt_text(), reply_markup=_redeem_prompt_kb())
        return
    await _do_redeem(msg, parts[1].split()[0])


def _awaiting_code_filter(_, __, m) -> bool:
    u = getattr(m, "from_user", None)
    if not u or not _awaiting(u.id):
        return False
    text = (getattr(m, "text", None) or "").strip()
    if not text:
        return False
    if text[:1] in "./!":
        _AWAIT_CODE.pop(u.id, None)  # user moved on to a command
        return False
    return True


# group=1: after force-sub gate (-2), before the login text flow (2), so a
# coupon code is never mistaken for a phone number / OTP.
@Client.on_message(filters.private & filters.text & filters.create(_awaiting_code_filter), group=1)
async def on_coupon_code_text(bot: Client, msg: Message):
    await _do_redeem(msg, msg.text.strip().split()[0])
    msg.stop_propagation()


# ── User: send payment screenshot ─────────────────────────

@Client.on_message(filters.photo & filters.private)
async def on_payment_screenshot(bot: Client, msg: Message):
    """When a user sends a photo and has a pending SS request, forward to owner + log."""
    if not msg.from_user:
        return
    user_id = msg.from_user.id
    pending = await _load_pending_ss()
    info = pending.pop(str(user_id), None)
    if not info:
        return
    await _save_pending_ss(pending)
    plan_id = info["plan_id"]
    request_id = info["request_id"]
    label = next((p[1] for p in PAYMENT_PLANS if p[0] == plan_id), plan_id)
    price = await get_plan_price(plan_id)

    caption = (
        f"📸 <b>Payment Screenshot</b>\n\n"
        f"{LINE}\n"
        f"👤 <b>User:</b> {_esc(msg.from_user.first_name or '—')}"
        f"{(' (@' + _esc(msg.from_user.username) + ')') if msg.from_user.username else ''}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"💳 <b>Plan:</b> {label} (<code>{plan_id}</code>)\n"
        f"💰 <b>Price:</b> <code>{_esc(str(price or '—'))}</code>\n"
        f"📋 <b>Request ID:</b> <code>{request_id}</code>\n"
        f"{LINE}\n\n"
        f"✅ Approve karne ke liye niche button dabayein."
    )

    approve_kb = K([
        [B(f"✅ Approve — {label}", callback_data=f"pay:approve:{request_id}"),
         B("❌ Reject", callback_data=f"pay:reject:{request_id}")],
    ])

    # Forward to all owners
    for owner_id in Config.OWNER_IDS:
        try:
            await bot.send_photo(
                owner_id, msg.photo.file_id,
                caption=caption, reply_markup=approve_kb,
            )
        except Exception:
            pass

    # Forward to log channel
    try:
        from helpers.logger_channel import get_channel
        log_cid = get_channel()
        if log_cid:
            await bot.send_photo(
                log_cid, msg.photo.file_id,
                caption=caption, reply_markup=approve_kb,
            )
    except Exception:
        pass

    await msg.reply_text(
        f"✅ <b>Screenshot received!</b>\n\n"
        f"Plan: <b>{label}</b>\n"
        f"Request ID: <code>{request_id}</code>\n\n"
        f"Owner ko payment screenshot bhej di gayi hai.\n"
        f"Approve hone par aapko turant message milega. 🎉",
        reply_markup=K([[B("⬅ Home", callback_data="menu:home")]]),
    )


# ── Callbacks ────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^pay:"))
async def cb_payment(bot, cq):
    parts = cq.data.split(":")
    action = parts[1]

    if action == "refresh":
        if not Config.is_owner(cq.from_user.id):
            await safe_answer(cq, "Sirf owner!", show_alert=True)
            return
        await edit_screen(cq.message, await _prices_text(),
                          reply_markup=payment_owner_kb())
        await safe_answer(cq, "Prices refreshed")

    elif action == "owner_panel":
        if not Config.is_owner(cq.from_user.id):
            await safe_answer(cq, "Sirf owner!", show_alert=True)
            return
        await edit_screen(cq.message, _payment_panel_text(),
                          reply_markup=payment_owner_kb())
        await safe_answer(cq)

    elif action == "pending":
        if not Config.is_owner(cq.from_user.id):
            await safe_answer(cq, "Sirf owner!", show_alert=True)
            return
        requests = await db.pending_payment_requests()
        if not requests:
            await edit_screen(cq.message, "📋 Koi pending request nahi hai.",
                              reply_markup=payment_owner_kb())
            await safe_answer(cq)
            return
        rows = []
        for r in requests[:8]:
            label = next((p[1] for p in PAYMENT_PLANS if p[0] == r["plan_id"]), r["plan_id"])
            rows.append([B(
                f"✅ {label} — {r['user_id']}",
                callback_data=f"pay:approve:{r['request_id']}",
            ), B(
                "❌ Reject",
                callback_data=f"pay:reject:{r['request_id']}",
            )])
        rows.append([B("⬅ Owner Panel", callback_data="pay:owner_panel")])
        await edit_screen(cq.message,
                          "📋 <b>Pending Payment Requests</b>\nApprove/Reject karein:",
                          reply_markup=K(rows))
        await safe_answer(cq)

    elif action == "approve":
        if not Config.is_owner(cq.from_user.id):
            await safe_answer(cq, "Sirf owner!", show_alert=True)
            return
        req_id = parts[2] if len(parts) > 2 else ""
        pre = await db.get_payment_request(req_id)
        if not pre or pre.get("plan_id") not in _VALID_PLANS:
            await safe_answer(cq, "Request nahi mili ya plan invalid hai.", show_alert=True)
            return
        # Atomic pending -> approved: double tap / two owners can't grant twice.
        req = await db.transition_payment_request(req_id, "approved", cq.from_user.id)
        if not req:
            await safe_answer(cq, "Yeh request pehle hi process ho chuki hai.", show_alert=True)
            return
        try:
            until_iso = await grant_premium(req["user_id"], req["plan_id"])
        except Exception:
            await db.update_payment_request(req_id, "pending", 0)
            await safe_answer(cq, "Premium grant fail hua, request pending rakhi hai.", show_alert=True)
            return
        label = plan_label(req["plan_id"])
        await safe_answer(cq, f"✅ Approved! {label} granted.", show_alert=True)
        try:
            await bot.send_message(
                req["user_id"],
                card("🎉", "Payment approved",
                     f"💎 Plan: <b>{label}</b>\n"
                     f"📅 Valid till: <b>{format_until(until_iso)}</b>\n"
                     "♾️ Ab aap unlimited uses kar sakte hain!"),
                reply_markup=K([[B("💎 My Plan", callback_data="pay:menu", style="success"),
                                 B("🏠 Home", callback_data="menu:home", style="primary")]]),
            )
        except Exception:
            pass
        await edit_screen(cq.message, f"✅ Approved: <code>{req_id}</code>\nUser: <code>{req['user_id']}</code>\nPlan: {label}",
                          reply_markup=payment_owner_kb())

    elif action == "reject":
        if not Config.is_owner(cq.from_user.id):
            await safe_answer(cq, "Sirf owner!", show_alert=True)
            return
        req_id = parts[2] if len(parts) > 2 else ""
        req = await db.transition_payment_request(req_id, "rejected", cq.from_user.id)
        if not req:
            await safe_answer(cq, "Request nahi mili ya already processed.", show_alert=True)
            return
        await safe_answer(cq, "❌ Rejected.", show_alert=True)
        try:
            await bot.send_message(
                req["user_id"],
                card("❌", "Payment request rejected",
                     "Aapka payment request reject ho gaya.\n"
                     "Dobara try karein ya owner se contact karein."),
                reply_markup=K([[B("💎 Plans", callback_data="pay:menu", style="primary")]]),
            )
        except Exception:
            pass
        await edit_screen(cq.message, f"❌ Rejected: <code>{req_id}</code>",
                          reply_markup=payment_owner_kb())

    elif action == "menu":
        config = await get_payment_config()
        await edit_screen(cq.message, await _user_plans_text(cq.from_user.id),
                          reply_markup=user_payment_kb(config))
        await safe_answer(cq)

    elif action == "plan":
        plan_id = parts[2] if len(parts) > 2 else ""
        if plan_id not in _VALID_PLANS:
            await safe_answer(cq, "Plan nahi mila.", show_alert=True)
            return
        label = plan_label(plan_id)
        price = await get_plan_price(plan_id)
        qr = await get_qr_code()

        body = f"💰 Price: <b>{_esc(str(price or '— not set —'))}</b>\n\n"
        if qr:
            body += ("<b>Kaise kharidein</b>\n"
                     "<b>①</b> 📨 <b>Request This Plan</b> dabao\n"
                     "<b>②</b> QR code DM me aayega — scan karke pay karo\n"
                     "<b>③</b> Payment ka <b>screenshot</b> yahin bhejo\n"
                     "<b>④</b> Owner approve karega — premium turant ON ✅")
        else:
            body += "⚠️ QR code abhi set nahi hai. Owner se contact karein."
        text = card("💎", label, body)

        kb_rows = []
        kb_rows.append([B("📨 Request This Plan", callback_data=f"pay:request:{plan_id}", style="success")])
        if PAYMENT_CONTACT_URL:
            kb_rows.append([B("💬 Contact Owner", url=PAYMENT_CONTACT_URL)])
        kb_rows.append([B("⬅ Back to Plans", callback_data="pay:menu")])
        await edit_screen(cq.message, text, reply_markup=K(kb_rows))
        await safe_answer(cq)

    elif action == "request":
        plan_id = parts[2] if len(parts) > 2 else ""
        if plan_id not in _VALID_PLANS:
            await safe_answer(cq, "Plan nahi mila.", show_alert=True)
            return
        label = plan_label(plan_id)
        price = await get_plan_price(plan_id)
        req_id = await db.create_payment_request(cq.from_user.id, plan_id)

        # Auto-show QR if available
        qr = await get_qr_code()
        if qr:
            try:
                await bot.send_photo(
                    cq.from_user.id, qr,
                    caption=(
                        f"💳 <b>Payment QR Code</b>\n\n"
                        f"Plan: <b>{label}</b>\n"
                        f"Price: <code>{price or '—'}</code>\n\n"
                        f"Is QR par payment karein, phir <b>payment ka screenshot</b> "
                        f"yahan bhejein. Owner turant approve karega!"
                    ),
                )
            except Exception:
                pass

        # Ask user for screenshot (persist in DB for restart safety)
        pending = await _load_pending_ss()
        pending[str(cq.from_user.id)] = {"plan_id": plan_id, "request_id": req_id}
        await _save_pending_ss(pending)

        await edit_screen(cq.message,
            card("📨", "Request created",
                 f"💎 Plan: <b>{label}</b>\n"
                 f"💰 Price: <b>{_esc(str(price or '—'))}</b>\n"
                 f"🧾 Request ID: <code>{req_id}</code>\n\n"
                 + ("✅ QR code DM me bhej diya.\n" if qr else "⚠️ QR set nahi hai — owner se contact karein.\n")
                 + "📸 <b>Ab payment ka screenshot yahin bhejo.</b>",
                 "Owner ko screenshot jayega, approve hote hi aapko message milega."),
            reply_markup=K([
                [B("💎 Plans", callback_data="pay:menu", style="primary"),
                 B("🏠 Home", callback_data="menu:home", style="primary")],
            ]))
        await safe_answer(cq, "Request sent! QR + screenshot instructions bheje!")

    elif action == "coupon":
        _AWAIT_CODE[cq.from_user.id] = time.time()
        try:  # VC "typing mode" would otherwise swallow the code
            from plugins import vc_tools as _vt
            _vt._typing.pop(cq.from_user.id, None)
        except Exception:
            pass
        await edit_screen(cq.message, _redeem_prompt_text(), reply_markup=_redeem_prompt_kb())
        await safe_answer(cq, "Code bhejo 👇")

    elif action == "coupon_cancel":
        _AWAIT_CODE.pop(cq.from_user.id, None)
        config = await get_payment_config()
        await edit_screen(cq.message, await _user_plans_text(cq.from_user.id),
                          reply_markup=user_payment_kb(config))
        await safe_answer(cq, "Cancelled")

    else:
        await safe_answer(cq)
