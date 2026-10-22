import re, sys

p = "plugins/vc_joiner.py"
s = open(p, encoding="utf-8").read()

# 1) Global semaphores + max join cap
if "_join_sem = asyncio.Semaphore" not in s:
    s = s.replace(
        "_lock = asyncio.Lock()",
        "_lock = asyncio.Lock()\n"
        "_join_sem = asyncio.Semaphore(2)\n"
        "_client_sem = asyncio.Semaphore(3)\n"
        "_MAX_JOIN = int(os.environ.get('VCJ_MAX_JOIN', '10'))",
        1,
    )
    print("added semaphores + cap")

# 2) Lazy-load clients with semaphore
old_load = """        if uvc is None:
            try:
                uvc = await session_manager.get(uid)
            except Exception as exc:
                logger.debug("lazy load %s failed: %r", uid, exc)
                uvc = None"""
new_load = """        if uvc is None:
            async with _client_sem:
                try:
                    uvc = await session_manager.get(uid)
                except Exception as exc:
                    logger.debug("lazy load %s failed: %r", uid, exc)
                    uvc = None"""
if old_load in s:
    s = s.replace(old_load, new_load, 1)
    print("patched lazy load semaphore")

# 3) FFmpeg threads = 1 (silent stream)
s = s.replace(
    'ffmpeg -hide_banner -loglevel error -nostdin -re ',
    'ffmpeg -hide_banner -loglevel error -nostdin -re -threads 1 ',
    1,
)

# 4) _join_all_target: concurrency + max cap
old_join = """        for uvc in uvs:
            if (uvc.owner_id, chat_id) in _joined:
                res["skipped"] += 1
                continue
            ok = await _join_one(uvc, chat_id)
            res["joined" if ok else "failed"] += 1
            await asyncio.sleep(2.0)  # rate limit
        return res"""
new_join = """        joined_count = 0
        for uvc in uvs:
            if (uvc.owner_id, chat_id) in _joined:
                res["skipped"] += 1
                continue
            if joined_count >= _MAX_JOIN:
                logger.warning("VCJ_MAX_JOIN=%s reached, skipping rest", _MAX_JOIN)
                break
            async with _join_sem:
                ok = await _join_one(uvc, chat_id)
            res["joined" if ok else "failed"] += 1
            if ok:
                joined_count += 1
            await asyncio.sleep(2.0)  # rate limit
        return res"""
if old_join in s:
    s = s.replace(old_join, new_join, 1)
    print("patched join concurrency + cap")

open(p, "w", encoding="utf-8").write(s)
print("DONE")
