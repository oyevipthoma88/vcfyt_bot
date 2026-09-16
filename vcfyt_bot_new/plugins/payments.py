"""Payment system: owner price/QR/coupon management + user plan display & requests.

Owner commands:
  /setprice <plan_id> <price>  — set plan price
  /grant <user_id> <plan_id>   — manually grant premium
  /revoke <user_id>            — revoke premium
  /addcoupon <code> <plan_id> [max_uses]  — create coupon
  /delcoupon <code>            — delete coupon
  /listcoupons                 — list all coupons
  /setqr (reply to photo)      — set QR code image
  /clearqr                     — remove QR code
  /pending                     — list pending payment requests

User commands:
  /redeem <code>               — redeem coupon code
  /plans                       — view plans & buy

Callbacks:
  pay:menu / pay:plan:<id> / pay:request:<id>
  pay:approve:<id> / pay:reject:<id>  (owner only)
  pay:coupon / pay:owner_panel
"""

import json

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K, Message

from config import Config
from helpers.access_control import (
    PAYMENT_CONTACT_URL, PAYMENT_PLANS,
    get_payment_config, get_plan_price, get_qr_code, grant_premium,
    premium_info, redeem_coupon, revoke_premium_all, set_payment_config, set_qr_code,
)
from helpers.database import db
from helpers.logger_channel import log_command
from plugins.ui import B, LINE, edit_screen, safe_answer

# Track users who are expected to send a payment screenshot
# Persisted in DB so it survives restarts.
SS_PENDING_KEY = "pending_payment_ss"


async def _load_pending_ss() -> dict:
    import json as _json
    raw = await db.get_app_value(SS_PENDING_KEY)
    if raw:
        try:
            return _json.loads(raw)
        except (ValueError, TypeError):
            pass
    return {}


async def _save_pending_ss(data: dict):
    import json as _json
    await db.set_app_value(SS_PENDING_KEY, _json.dumps(data))


# ── Owner: price & coupon panel ──────────────────────────

def _payment_panel_text() -> str:
    return (
        "💳 <b>Payment Management</b>\n\n"
        f"{LINE}\n"
        "<b>Owner Commands:</b>\n"
        "• <code>/setprice &lt;plan_id&gt; &lt;price&gt;</code>\n"
        "• <code>/grant &lt;user_id&gt; &lt;plan_id&gt;</code>\n"
        "• <code>/revoke &lt;user_id&gt;</code>\n"
        "• <code>/addcoupon &lt;code&gt; &lt;plan_id&gt; [max_uses]</code>\n"
        "• <code>/delcoupon &lt;code&gt;</code>\n"
        "• <code>/listcoupons</code>\n"
        "• <code>/setqr</code> (QR photo reply karke)\n"
        "• <code>/clearqr</code>\n"
        "• <code>/pending</code> — pending payment requests\n\n"
        "<b>Plans:</b>\n"
        + "\n".join(f"• <code>{p[0]}</code> — {p[1]}" for p in PAYMENT_PLANS)
    )


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
        price = config.get(plan_id, {}).get("price", "— not set —")
        lines.append(f"• <b>{label}</b> (<code>{plan_id}</code>): <code>{price}</code>")
    return "💳 <b>Payment Plans & Prices</b>\n\n" + "\n".join(lines)


# ── User: plans display ──────────────────────────────────

async def _user_plans_text(user_id: int) -> str:
    config = await get_payment_config()
    pinfo = await premium_info(user_id)

    header = ""
    if pinfo["active"]:
        header = (
            f"✅ <b>Premium Active</b>\n"
            f"📋 Plan: <code>{pinfo['plan']}</code>\n"
            f"⏰ Until: <code>{pinfo['until']}</code>\n\n"
        )
    else:
        header = "⚠️ <b>Free Plan — Daily Limit Active</b>\n\n"

    lines = []
    for plan_id, label, _, _ in PAYMENT_PLANS:
        entry = config.get(plan_id)
        price = entry.get("price", "—") if entry else "—"
        lines.append(f"• <b>{label}</b> — <code>{price}</code>  (<code>{plan_id}</code>)")

    return (
        f"💳 <b>Upgrade to Premium</b>\n\n"
        f"{header}"
        f"{LINE}\n"
        + "\n".join(lines)
        + f"\n{LINE}\n\n"
        "Premium = <b>unlimited</b> uses, no daily limit!\n"
        "Plan choose karein → Request karein → QR DM mein aayega → payment karein → screenshot bhejein.\n"
        "Ya coupon code redeem karein: <code>/redeem &lt;code&gt;</code>"
    )


def user_payment_kb(config: dict = None) -> K:
    rows = []
    plan_rows = []
    for plan_id, label, _, _ in PAYMENT_PLANS:
        if config:
            entry = config.get(plan_id)
            price = entry.get("price", "—") if entry else "—"
        else:
            price = "—"
        plan_rows.append([B(f" {label} — {price}", callback_data=f"pay:plan:{plan_id}")])
    rows.extend(plan_rows[:3])
    rows.extend(plan_rows[3:])
    rows.append([B("🎟️ Redeem Coupon", callback_data="pay:coupon")])
    rows.append([B("⬅ Home", callback_data="menu:home")])
    return K(rows)


# ── Owner commands ───────────────────────────────────────

@Client.on_message(filters.command("setprice") & filters.private)
async def cmd_setprice(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: <code>/setprice &lt;plan_id&gt; &lt;price&gt;</code>\n\n"
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


@Client.on_message(filters.command("grant") & filters.private)
async def cmd_grant(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: <code>/grant &lt;user_id&gt; &lt;plan_id&gt;</code>\n"
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


@Client.on_message(filters.command("revoke") & filters.private)
async def cmd_revoke(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply_text("Usage: <code>/revoke &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    ok = await revoke_premium_all(uid)
    await msg.reply_text(
        f"✅ Premium revoked for <code>{uid}</code>" if ok
        else f"❌ User <code>{uid}</code> ke paas premium nahi tha."
    )


@Client.on_message(filters.command("addcoupon") & filters.private)
async def cmd_addcoupon(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=3)
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: <code>/addcoupon &lt;code&gt; &lt;plan_id&gt; [max_uses]</code>\n"
            "Plans: " + ", ".join(p[0] for p in PAYMENT_PLANS)
        )
        return
    code = parts[1].strip().upper()
    plan_id = parts[2].strip().lower()
    max_uses = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    valid_ids = {p[0] for p in PAYMENT_PLANS}
    if plan_id not in valid_ids:
        await msg.reply_text(f"❌ Invalid plan. Valid: {', '.join(sorted(valid_ids))}")
        return
    ok = await db.create_coupon(code, plan_id, max_uses, msg.from_user.id)
    if ok:
        await msg.reply_text(
            f"✅ Coupon created!\nCode: <code>{code}</code>\n"
            f"Plan: <code>{plan_id}</code>\nMax uses: {max_uses}"
        )
    else:
        await msg.reply_text(f"❌ Coupon <code>{code}</code> already exists.")


@Client.on_message(filters.command("delcoupon") & filters.private)
async def cmd_delcoupon(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>/delcoupon &lt;code&gt;</code>")
        return
    ok = await db.delete_coupon(parts[1])
    await msg.reply_text(
        f"✅ Coupon deleted: <code>{parts[1].upper()}</code>" if ok
        else f"❌ Coupon nahi mila."
    )


@Client.on_message(filters.command("listcoupons") & filters.private)
async def cmd_listcoupons(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    coupons = await db.all_coupons()
    if not coupons:
        await msg.reply_text("📋 Koi coupon nahi hai.")
        return
    lines = []
    for c in coupons:
        lines.append(
            f"• <code>{c['code']}</code> — {c['plan_id']} "
            f"({c.get('used_count', 0)}/{c.get('max_uses', 1)})"
        )
    await msg.reply_text("📋 <b>All Coupons</b>\n" + "\n".join(lines))


@Client.on_message(filters.command("setqr") & filters.private)
async def cmd_setqr(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    reply = msg.reply_to_message
    if not reply or not (reply.photo or (reply.document and reply.document.mime_type and "image" in reply.document.mime_type)):
        await msg.reply_text(
            "❌ <b>QR code set karne ka tareeka:</b>\n\n"
            "1. QR code ki photo bot ko bhejein\n"
            "2. Us photo ko reply karke <code>/setqr</code> likhein\n\n"
            "Ya agar photo already bheji hai, to us par reply karke command do."
        )
        return
    photo = reply.photo
    file_id = photo.file_id if photo else reply.document.file_id
    await set_qr_code(file_id)
    await msg.reply_text("✅ QR code set ho gaya. Ab users plan select karke request karte hi QR DM mein mil jayega.")


@Client.on_message(filters.command("clearqr") & filters.private)
async def cmd_clearqr(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    await set_qr_code("")
    await msg.reply_text("✅ QR code removed.")


@Client.on_message(filters.command("pending") & filters.private)
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

@Client.on_message(filters.command("redeem") & filters.private)
async def cmd_redeem(bot: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>/redeem &lt;coupon_code&gt;</code>")
        return
    result = await redeem_coupon(msg.from_user.id, parts[1])
    await msg.reply_text(result["message"])


# ── User: send payment screenshot ─────────────────────────

@Client.on_message(filters.photo & filters.private)
async def on_payment_screenshot(bot: Client, msg: Message):
    """When a user sends a photo and has a pending SS request, forward to owner + log."""
    user_id = msg.from_user.id
    pending = await _load_pending_ss()
    if user_id not in pending:
        return
    info = pending.pop(user_id)
    await _save_pending_ss(pending)
    plan_id = info["plan_id"]
    request_id = info["request_id"]
    label = next((p[1] for p in PAYMENT_PLANS if p[0] == plan_id), plan_id)
    price = await get_plan_price(plan_id)

    caption = (
        f"📸 <b>Payment Screenshot</b>\n\n"
        f"{LINE}\n"
        f"👤 <b>User:</b> {msg.from_user.first_name or '—'}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"💳 <b>Plan:</b> {label} (<code>{plan_id}</code>)\n"
        f"💰 <b>Price:</b> <code>{price or '—'}</code>\n"
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
                f"❌ Reject",
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
        req_id = parts[2]
        req = await db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await safe_answer(cq, "Request nahi mili ya already processed.", show_alert=True)
            return
        try:
            until_iso = await grant_premium(req["user_id"], req["plan_id"])
        except ValueError:
            await safe_answer(cq, "Invalid plan!", show_alert=True)
            return
        await db.update_payment_request(req_id, "approved", cq.from_user.id)
        label = next((p[1] for p in PAYMENT_PLANS if p[0] == req["plan_id"]), req["plan_id"])
        await safe_answer(cq, f"✅ Approved! {label} granted.", show_alert=True)
        try:
            await bot.send_message(
                req["user_id"],
                f"🎉 <b>Payment Approved!</b>\n"
                f"Plan: <b>{label}</b>\nUntil: <code>{until_iso}</code>\n\n"
                "Ab aap unlimited uses kar sakte hain!"
            )
        except Exception:
            pass
        await edit_screen(cq.message, f"✅ Approved: <code>{req_id}</code>\nUser: <code>{req['user_id']}</code>\nPlan: {label}",
                          reply_markup=payment_owner_kb())

    elif action == "reject":
        if not Config.is_owner(cq.from_user.id):
            await safe_answer(cq, "Sirf owner!", show_alert=True)
            return
        req_id = parts[2]
        req = await db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await safe_answer(cq, "Request nahi mili ya already processed.", show_alert=True)
            return
        await db.update_payment_request(req_id, "rejected", cq.from_user.id)
        await safe_answer(cq, "❌ Rejected.", show_alert=True)
        try:
            await bot.send_message(
                req["user_id"],
                "❌ <b>Payment Request Rejected</b>\n\n"
                "Aapka payment request reject ho gaya. "
                "Dobarra try karein ya owner se contact karein."
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
        plan_id = parts[2]
        label = next((p[1] for p in PAYMENT_PLANS if p[0] == plan_id), plan_id)
        price = await get_plan_price(plan_id)
        qr = await get_qr_code()

        text = (
            f"💳 <b>{label}</b>\n\n"
            f"{LINE}\n"
            f"💰 <b>Price:</b> <code>{price or '— not set —'}</code>\n"
            f"{LINE}\n\n"
        )

        if qr:
            text += (
                "📸 <b>Payment Steps:</b>\n"
                f"1. Niche <b>📨 Request This Plan</b> button dabayein\n"
                f"2. QR code aapko DM mein bhej diya jayega\n"
                f"3. QR scan karke payment karein\n"
                f"4. Payment ka <b>screenshot</b> yahan bhejein\n"
                f"5. Owner approve karega — premium turant activate!\n\n"
            )
        else:
            text += (
                "⚠️ QR code abhi set nahi hai. Owner se contact karein.\n\n"
            )

        kb_rows = []
        kb_rows.append([B("📨 Request This Plan", callback_data=f"pay:request:{plan_id}")])
        if PAYMENT_CONTACT_URL:
            kb_rows.append([B("💬 Contact Owner", url=PAYMENT_CONTACT_URL)])
        kb_rows.append([B("⬅ Back to Plans", callback_data="pay:menu")])
        await edit_screen(cq.message, text, reply_markup=K(kb_rows))
        await safe_answer(cq)

    elif action == "request":
        plan_id = parts[2]
        label = next((p[1] for p in PAYMENT_PLANS if p[0] == plan_id), plan_id)
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
        pending[cq.from_user.id] = {"plan_id": plan_id, "request_id": req_id}
        await _save_pending_ss(pending)

        await edit_screen(cq.message,
            f"📨 <b>Payment Request Created!</b>\n\n"
            f"{LINE}\n"
            f"💳 Plan: <b>{label}</b>\n"
            f"💰 Price: <code>{price or '—'}</code>\n"
            f"📋 Request ID: <code>{req_id}</code>\n"
            f"{LINE}\n\n"
            f"{'✅ QR code DM mein bhej diya!' if qr else '⚠️ QR set nahi hai.'}\n\n"
            f"📸 <b>Ab payment ka screenshot yahan bhejein.</b>\n"
            f"Owner ko screenshot bhej di jayegi aur approve/reject karega.",
            reply_markup=K([
                [B("⬅ Plans", callback_data="pay:menu")],
                [B("⬅ Home", callback_data="menu:home")],
            ]))
        await safe_answer(cq, "Request sent! QR + screenshot instructions bheje!")

    elif action == "coupon":
        await edit_screen(cq.message,
            "🎟️ <b>Coupon Redeem</b>\n\n"
            "Apna coupon code bhejein:\n"
            "<code>/redeem &lt;code&gt;</code>",
            reply_markup=K([[B("⬅ Back to Plans", callback_data="pay:menu")]]))
        await safe_answer(cq)
