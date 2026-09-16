"""Referral system: link generation, tracking, milestone rewards.

Referral tiers:
  7 referrals  → 5 hours premium
  15 referrals → 10 hours premium
  25 referrals → 24 hours premium
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K, Message

from config import Config
from helpers.access_control import REFERRAL_REWARDS, check_referral_milestones, premium_info
from helpers.database import db
from plugins.ui import B, LINE, edit_screen, safe_answer

logger = logging.getLogger("vcbot.referral")


def _referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def _referral_text(user_id: int, bot_username: str) -> str:
    stats = await db.referral_stats(user_id)
    count = stats["count"]
    pinfo = await premium_info(user_id)

    lines = []
    for milestone, hours in REFERRAL_REWARDS:
        claimed = stats["claimed"].get(milestone, False)
        done = count >= milestone
        if claimed:
            icon = "✅"
        elif done:
            icon = "🎁"
        else:
            icon = "⬜"
        lines.append(f"  {icon} <b>{milestone} referrals</b> → {hours} hrs premium")

    premium_line = ""
    if pinfo["active"]:
        premium_line = f"\n\n✅ <b>Premium Active</b> until <code>{pinfo['until']}</code>"

    return (
        f"👥 <b>Referral Program</b>\n\n"
        f"{LINE}\n"
        f"Apna referral link share karo aur dosto ko bot par laao!\n"
        f"Jitne zyada referrals, utni zyada premium hours!\n\n"
        f"📊 <b>Your Referrals:</b> {count}\n\n"
        + "\n".join(lines)
        + f"\n\n{LINE}\n"
        f"🔗 <b>Your Referral Link:</b>\n"
        f"<code>{_referral_link(bot_username, user_id)}</code>"
        + premium_line
        + f"\n\n{LINE}\n"
        f"💡 <b>How it works:</b>\n"
        f"• Apna link dosto ko share karein\n"
        f"• Wo bot par /start link se aayein\n"
        f"• Referral count automatically badhega\n"
        f"• Milestone poora hone par free premium hours milega!"
    )


def referral_kb(bot_username: str, user_id: int) -> K:
    link = _referral_link(bot_username, user_id)
    return K([
        [B("🔗 Share Referral Link", url=f"https://t.me/share/url?url={link}&text=Join%20this%20awesome%20VC%20bot!")],
        [B("💳 View Premium Plans", callback_data="pay:menu")],
        [B("🎟️ Redeem Coupon", callback_data="pay:coupon")],
        [B("⬅ Home", callback_data="menu:home")],
    ])


# ── Handlers ─────────────────────────────────────────────

@Client.on_message(filters.command("referral") & filters.private)
async def cmd_referral(bot: Client, msg: Message):
    me = await bot.get_me()
    await edit_or_send(
        msg,
        await _referral_text(msg.from_user.id, me.username),
        reply_markup=referral_kb(me.username, msg.from_user.id),
    )


@Client.on_callback_query(filters.regex(r"^ref:"))
async def cb_referral(bot, cq):
    me = await bot.get_me()
    await edit_screen(cq.message,
                      await _referral_text(cq.from_user.id, me.username),
                      reply_markup=referral_kb(me.username, cq.from_user.id))
    await safe_answer(cq)


async def edit_or_send(msg: Message, text: str, reply_markup=None):
    try:
        await msg.reply_text(text, reply_markup=reply_markup,
                             disable_web_page_preview=True)
    except Exception:
        pass


async def process_referral_start(bot: Client, user_id: int, referrer_id: int):
    """Record a referral if valid and check milestones.

    Called from /start when the payload is ref_<referrer_id>.
    """
    if user_id == referrer_id:
        return
    added = await db.add_referral(referrer_id, user_id)
    if not added:
        return
    claimed = await check_referral_milestones(referrer_id)
    if claimed:
        try:
            text_parts = []
            for milestone, hours in claimed:
                text_parts.append(f"🎁 {milestone} referrals complete! {hours} hours premium mil gaya!")
            await bot.send_message(referrer_id, "🎉 <b>Referral Reward!</b>\n\n" + "\n".join(text_parts))
        except Exception:
            pass
