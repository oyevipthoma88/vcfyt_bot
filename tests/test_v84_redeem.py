"""v84: coupon redeem rules (root fix for "ek code bahut baar use ho raha hai").

Rules under test:
  * ek user ek code sirf 1 baar (chahe max_uses kitna bhi ho)
  * total uses kabhi max_uses se zyada nahi, parallel taps me bhi
  * same-session linked IDs ek hi user gine jaate hain
  * purane redeem_logs wale users dobara redeem nahi kar sakte
  * Mongo coupon + purani SQLite copy: SQLite copy kabhi use nahi hoti
"""
import asyncio
import os

import pytest

from helpers import access_control as ac
from helpers import database as dbmod


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    d = dbmod.Database.__new__(dbmod.Database)
    d._mongo = None
    d._sqlite_path = str(tmp_path / "t.db")
    d._use_mongo = False
    d._purging = False
    d._quota_blocked_until = 0.0
    d._last_purge = 0.0
    d._init_sqlite()
    monkeypatch.setattr(ac, "db", d)
    monkeypatch.setattr(ac.Config, "is_owner", staticmethod(lambda uid: False))
    ac._REDEEM_FAILS.clear()
    ac._REDEEM_LOCKS.clear()
    return d


def _mongo_db(fresh_db):
    mm = pytest.importorskip("mongomock_motor")
    fresh_db._use_mongo = True
    fresh_db._mongo = mm.AsyncMongoMockClient()["vc_bot"]
    return fresh_db


def test_same_user_cannot_redeem_twice(fresh_db):
    assert _run(fresh_db.create_coupon("multi", "7day", 5)) is True
    first = _run(ac.redeem_coupon(1, "MULTI"))
    second = _run(ac.redeem_coupon(1, "multi"))
    assert first["ok"] is True
    assert second["ok"] is False and second["reason"] == "already"
    assert _run(fresh_db.get_coupon("MULTI"))["used_count"] == 1


def test_max_uses_is_a_hard_limit(fresh_db):
    _run(fresh_db.create_coupon("TWO", "1day", 2))
    results = [_run(ac.redeem_coupon(uid, "TWO"))["ok"] for uid in (1, 2, 3)]
    assert results == [True, True, False]
    assert _run(fresh_db.get_coupon("TWO"))["used_count"] == 2


def test_parallel_taps_single_use_only_one_wins(fresh_db):
    _run(fresh_db.create_coupon("ONCE", "1day", 1))

    async def burst():
        return await asyncio.gather(*[ac.redeem_coupon(uid, "ONCE") for uid in range(100, 130)])

    results = _run(burst())
    assert sum(1 for r in results if r["ok"]) == 1
    assert _run(fresh_db.get_coupon("ONCE"))["used_count"] == 1


def test_parallel_taps_same_user_only_one_wins(fresh_db):
    _run(fresh_db.create_coupon("SPAM", "1day", 50))

    async def burst():
        return await asyncio.gather(*[ac.redeem_coupon(7, "SPAM") for _ in range(20)])

    assert sum(1 for r in _run(burst()) if r["ok"]) == 1


def test_linked_account_counts_as_same_user(fresh_db, monkeypatch):
    _run(fresh_db.create_coupon("LINK", "1day", 10))
    async def linked(uid):
        return [11, 12]
    monkeypatch.setattr(fresh_db, "get_linked_user_ids", linked)
    assert _run(ac.redeem_coupon(11, "LINK"))["ok"] is True
    r = _run(ac.redeem_coupon(12, "LINK"))
    assert r["ok"] is False and r["reason"] == "already"


def test_legacy_redeem_log_blocks_repeat(fresh_db):
    _run(fresh_db.create_coupon("OLD", "1day", 10))
    _run(fresh_db.log_redeem("OLD", "1day", 55))  # redeemed before the fix
    r = _run(ac.redeem_coupon(55, "OLD"))
    assert r["ok"] is False and r["reason"] == "already"


def test_unknown_code_and_guess_limit(fresh_db):
    for _ in range(ac.REDEEM_MAX_FAILS):
        assert _run(ac.redeem_coupon(9, "NOPE"))["reason"] == "invalid"
    assert _run(ac.redeem_coupon(9, "NOPE"))["reason"] == "rate_limited"


def test_failed_grant_gives_the_use_back(fresh_db, monkeypatch):
    _run(fresh_db.create_coupon("BACK", "1day", 1))
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ac, "grant_premium", boom)
    assert _run(ac.redeem_coupon(1, "BACK"))["ok"] is False
    assert _run(fresh_db.get_coupon("BACK"))["used_count"] == 0
    monkeypatch.undo()


def test_redeem_actually_grants_premium(fresh_db):
    _run(fresh_db.create_coupon("PREM", "7day", 1))
    assert _run(ac.redeem_coupon(42, "PREM"))["ok"] is True
    assert _run(ac.is_premium(42)) is True


def test_mongo_coupon_ignores_stale_sqlite_copy(fresh_db):
    d = _mongo_db(fresh_db)
    _run(d.create_coupon("SPLIT", "1day", 1))
    # Legacy builds mirrored every coupon into SQLite with used_count=0.
    d._sql("INSERT INTO coupons (code, plan_id, max_uses, used_count, created_at, created_by) "
           "VALUES ('SPLIT','1day',1,0,'x',0)")
    assert _run(ac.redeem_coupon(1, "SPLIT"))["ok"] is True
    r = _run(ac.redeem_coupon(2, "SPLIT"))
    assert r["ok"] is False and r["reason"] == "exhausted"


def test_mongo_down_refuses_instead_of_falling_back(fresh_db):
    d = _mongo_db(fresh_db)
    _run(d.create_coupon("DOWN", "1day", 5))
    d._sql("INSERT INTO coupons (code, plan_id, max_uses, used_count, created_at, created_by) "
           "VALUES ('DOWN','1day',5,0,'x',0)")

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("mongo unreachable")
    d._mongo = Broken()
    r = _run(ac.redeem_coupon(3, "DOWN"))
    assert r["ok"] is False
    assert d._sql("SELECT used_count FROM coupons WHERE code='DOWN'", fetch=True)[0]["used_count"] == 0


def test_mongo_parallel_single_use(fresh_db):
    d = _mongo_db(fresh_db)
    _run(d.create_coupon("MONCE", "1day", 1))

    async def burst():
        return await asyncio.gather(*[ac.redeem_coupon(uid, "MONCE") for uid in range(200, 220)])

    assert sum(1 for r in _run(burst()) if r["ok"]) == 1


def test_payment_request_approves_only_once(fresh_db):
    rid = _run(fresh_db.create_payment_request(5, "7day"))
    assert _run(fresh_db.transition_payment_request(rid, "approved", 1)) is not None
    assert _run(fresh_db.transition_payment_request(rid, "approved", 1)) is None


def test_revoked_premium_does_not_come_back(fresh_db):
    d = _mongo_db(fresh_db)
    _run(ac.grant_premium(77, "7day"))
    d._sql("INSERT OR REPLACE INTO premium VALUES (77,'2999-01-01T00:00:00+00:00','7day','x')")
    assert _run(ac.revoke_premium_all(77)) is True
    assert _run(ac.is_premium(77)) is False


def test_payment_screenshot_pending_survives_json(fresh_db, monkeypatch):
    payments = pytest.importorskip("plugins.payments")
    monkeypatch.setattr(payments, "db", fresh_db)
    _run(payments._save_pending_ss({123: {"plan_id": "7day", "request_id": "r1"}}))
    loaded = _run(payments._load_pending_ss())
    assert loaded.get("123", {}).get("request_id") == "r1"
