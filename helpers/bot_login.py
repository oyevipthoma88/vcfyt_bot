"""Bot login jo Telegram ke "cold login" outage se bacha rahe.

ROOT CAUSE (Nov 2026 logs):
    auth.ImportBotAuthorization -> [500 RPC_CALL_FAIL]

Heroku ka filesystem har restart / deploy / daily cycle par mit jata hai.
Purana code bot ko file session ("vcbot.session") se chalata tha, isliye har
boot par bot ko Telegram me *naya* login (ImportBotAuthorization) karna padta
tha. Jab Telegram ka bot-login server kharab ho (DC1 RPC_CALL_FAIL outage,
tdlib/telegram-bot-api#1103) to naya login fail hota hai — jabki pehle se
logged-in bots theek chalte rehte hain. Upar se har fail par 11 baar turant
retry + session wipe, jo Telegram ko aur flood karta tha (429 retry after 32).

FIX:
  1. Login hone ke baad bot ka session MongoDB me save (token + API_ID ke
     fingerprint ke saath). Agle boot par wahi session -> koi naya login nahi.
  2. Session sirf tab hataya jata hai jab Telegram bole key mar chuki hai
     (401 / AUTH_KEY_DUPLICATED) ya token badal gaya. 5xx / network par nahi.
  3. Naya login sirf 1 internal retry ke saath, phir dheere-dheere backoff
     (15s -> 5 min, jitter) taaki Telegram ka flood-limit aur na bigde.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("vcbot.bot_login")

KEY_PREFIX = "bot_session:"
# Optional manual override (e.g. doosre server se export kiya hua bot session).
ENV_OVERRIDE = {"main": "BOT_SESSION_STRING"}

MakeClient = Callable[..., object]


# ── classification ──────────────────────────────────────────────────────────

def _err_text(exc: BaseException) -> str:
    return f"{type(exc).__name__} {exc}".upper()


def is_dead_session_error(exc: BaseException) -> bool:
    """Saved auth key ab kaam ki nahi (logout / revoke / duplicate)."""
    try:
        from pyrogram.errors import AuthKeyDuplicated, Unauthorized
        if isinstance(exc, (Unauthorized, AuthKeyDuplicated)):
            return True
    except Exception:
        pass
    text = _err_text(exc)
    return any(k in text for k in (
        "AUTH_KEY_UNREGISTERED", "AUTH_KEY_INVALID", "AUTH_KEY_DUPLICATED",
        "SESSION_REVOKED", "SESSION_EXPIRED", "USER_DEACTIVATED",
    ))


def is_bad_token_error(exc: BaseException) -> bool:
    text = _err_text(exc)
    return any(k in text for k in ("ACCESS_TOKEN_INVALID", "ACCESS_TOKEN_EXPIRED", "BOT_TOKEN_INVALID"))


def is_transient_error(exc: BaseException) -> bool:
    """Telegram 5xx (RPC_CALL_FAIL etc.), timeouts, network drops."""
    try:
        from pyrogram.errors import InternalServerError, ServiceUnavailable
        if isinstance(exc, (InternalServerError, ServiceUnavailable)):
            return True
    except Exception:
        pass
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    text = _err_text(exc)
    return any(k in text for k in ("RPC_CALL_FAIL", "INTERNAL", "500", "TIMEOUT", "-503", "-500"))


def flood_wait_seconds(exc: BaseException) -> Optional[int]:
    try:
        from pyrogram.errors import FloodWait
        if isinstance(exc, FloodWait):
            return max(1, int(getattr(exc, "value", 1) or 1))
    except Exception:
        pass
    return None


def backoff_seconds(attempt: int, cap: int = 300) -> float:
    """15, 30, 60, 120, 240, 300, 300 ... (+0-20% jitter)."""
    base = min(cap, 15 * (2 ** max(0, attempt - 1)))
    return base + random.uniform(0, base * 0.2)


# ── saved session storage ───────────────────────────────────────────────────

def fingerprint(bot_token: str, api_id) -> str:
    raw = f"{int(api_id or 0)}:{(bot_token or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _db():
    from helpers.database import db
    return db


async def load_saved_session(key: str, bot_token: str, api_id) -> Optional[str]:
    env_name = ENV_OVERRIDE.get(key)
    if env_name:
        env_val = (os.environ.get(env_name) or "").strip()
        if env_val:
            return env_val
    try:
        raw = await _db().get_app_value(KEY_PREFIX + key)
    except Exception as exc:
        logger.warning("Saved bot session read failed: %r", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("session"):
        return None
    if data.get("fp") != fingerprint(bot_token, api_id):
        # BOT_TOKEN ya API_ID badla -> purana session is bot ka nahi raha.
        await forget_saved_session(key)
        return None
    return str(data["session"])


async def save_session(key: str, bot_token: str, api_id, session: str) -> None:
    if not session:
        return
    try:
        await _db().set_app_value(KEY_PREFIX + key, json.dumps(
            {"fp": fingerprint(bot_token, api_id), "session": session}))
    except Exception as exc:
        logger.warning("Bot session save failed: %r", exc)


async def forget_saved_session(key: str) -> None:
    try:
        await _db().delete_app_value(KEY_PREFIX + key)
    except Exception:
        pass


# ── login ───────────────────────────────────────────────────────────────────

def _limit_bot_auth_retries(client) -> None:
    """Pyrogram ImportBotAuthorization ko 500 par 10 baar turant dohrata hai
    (0.5s gap). Outage me ye flood banata hai; humara loop khud backoff karta hai."""
    try:
        from pyrogram import raw
    except Exception:
        return
    original = client.invoke

    async def invoke(query, *args, **kwargs):
        if isinstance(query, raw.functions.auth.ImportBotAuthorization):
            if args:
                args = (min(int(args[0]), 1), *args[1:])
            else:
                kwargs["retries"] = min(int(kwargs.get("retries", 1)), 1)
        return await original(query, *args, **kwargs)

    client.invoke = invoke


async def _safe_stop(client) -> None:
    for name in ("stop", "disconnect"):
        try:
            await getattr(client, name)()
            return
        except Exception:
            continue


async def start_once(make_client: MakeClient, *, key: str, bot_token: str, api_id):
    """Ek koshish: saved session (agar hai) warna naya login.

    Return: started client. Error aage raise hota hai (caller retry kare).
    Saved session se start par Telegram ka bot-login server touch hi nahi hota.
    """
    saved = await load_saved_session(key, bot_token, api_id)
    if saved:
        client = make_client(session_string=saved)
        try:
            await client.start()
            return client
        except BaseException as exc:
            await _safe_stop(client)
            if not is_dead_session_error(exc):
                raise  # 5xx / network: session sahi hai, rakho aur baad me retry
            logger.warning("Saved bot session expired (%s); naya login hoga", type(exc).__name__)
            await forget_saved_session(key)

    client = make_client(session_string=None)
    _limit_bot_auth_retries(client)
    try:
        await client.start()
    except BaseException:
        await _safe_stop(client)
        raise
    try:
        await save_session(key, bot_token, api_id, await client.export_session_string())
        logger.info("Bot session saved — agle restart par naya Telegram login nahi lagega")
    except Exception as exc:
        logger.warning("Could not export bot session: %r", exc)
    return client


TokenCheck = Callable[[], Awaitable[str]]


async def start_forever(make_client: MakeClient, *, key: str, bot_token: str, api_id,
                        token_check: Optional[TokenCheck] = None,
                        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
    """Bot online hone tak retry. Galat token par bhi crash-loop nahi (dyno chalta rahe)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            client = await start_once(make_client, key=key, bot_token=bot_token, api_id=api_id)
            if attempt > 1:
                logger.info("Bot login OK after %s attempts", attempt)
            return client
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            flood = flood_wait_seconds(exc)
            if flood is not None:
                wait = flood + 5
                logger.warning("Telegram FloodWait on bot login; retry %s in %ss", attempt, wait)
            elif is_bad_token_error(exc):
                wait = 300
                logger.error("BOT_TOKEN galat ya revoke ho chuka hai — BotFather se naya token lekar "
                             "Heroku Config Vars me BOT_TOKEN update karo. (%s)", exc)
            elif is_transient_error(exc) or is_dead_session_error(exc):
                wait = backoff_seconds(attempt)
                if token_check and (attempt == 1 or attempt % 10 == 0):
                    state = await token_check()
                    if state == "invalid":
                        wait = 300
                        logger.error("BOT_TOKEN galat ya revoke ho chuka hai — BotFather se naya token "
                                     "lekar Heroku Config Vars me BOT_TOKEN update karo.")
                    elif state in ("ok", "busy"):
                        logger.warning("BOT_TOKEN sahi hai; Telegram ka bot-login server abhi naya login "
                                       "reject kar raha hai (Telegram ki taraf ka outage). Bot khud retry "
                                       "karta rahega — kuch karne ki zaroorat nahi.")
                logger.warning("Telegram temporary error during bot login (%s: %s); retry %s in %ss",
                               type(exc).__name__, exc, attempt, int(wait))
            else:
                wait = backoff_seconds(attempt)
                logger.error("Bot login failed (%s: %s); retry %s in %ss",
                             type(exc).__name__, exc, attempt, int(wait))
            await sleep(wait)
