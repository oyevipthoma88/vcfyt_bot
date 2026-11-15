"""v90: Sudo / free-trial rules for users who have NOT logged in.

  * free trial = 3 uses (owner can change), only after pressing Free Trial
  * trial exhausted -> trial_over (no more uses)
  * own login always wins (self), owner always owner
  * owner-granted sudo and premium are unlimited (no trial credits used)
  * 5 referrals -> premium -> sudo access
  * failed play refunds the trial credit
"""
import asyncio

import pytest

from helpers import access_control as ac
from helpers import database as dbmod
from helpers import sudo


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
    monkeypatch.setattr(sudo, "db", d)
    monkeypatch.setattr(ac.Config, "is_owner", staticmethod(lambda uid: uid == 1))
    sudo._cache_clear()
    sudo._trial_locks.clear()
    return d


def test_trial_needs_button_and_gives_three_uses(fresh_db):
    assert _run(sudo.engine_mode(50)) == sudo.NONE
    ok, _ = _run(sudo.consume_trial(50))
    assert ok is False                      # not started yet
    _run(sudo.start_trial(50))
    assert _run(sudo.engine_mode(50, fresh=True)) == sudo.TRIAL
    results = [_run(sudo.consume_trial(50))[0] for _ in range(4)]
    assert results == [True, True, True, False]
    assert _run(sudo.engine_mode(50, fresh=True)) == sudo.TRIAL_OVER


def test_owner_can_change_trial_uses(fresh_db):
    _run(sudo.update_settings(trial_uses=5))
    _run(sudo.start_trial(51))
    assert [_run(sudo.consume_trial(51))[0] for _ in range(6)] == [True] * 5 + [False]


def test_failed_play_refunds_trial_credit(fresh_db):
    _run(sudo.start_trial(52))
    _run(sudo.consume_trial(52))
    _run(sudo.refund_trial(52))
    assert _run(sudo.trial_state(52))["left"] == 3


def test_own_login_and_owner_never_use_sudo(fresh_db):
    _run(fresh_db.add_user(60, "u", "U", "x" * 200))
    assert _run(sudo.engine_mode(60, fresh=True)) == sudo.SELF
    assert _run(sudo.engine_mode(1, fresh=True)) == sudo.OWNER
    assert _run(sudo.is_sudo_mode(60)) is False


def test_owner_granted_sudo_is_unlimited_and_revocable(fresh_db):
    _run(sudo.grant_sudo(70, days=7))
    assert _run(sudo.engine_mode(70, fresh=True)) == sudo.SUDO
    assert sudo.SUDO in sudo.UNLIMITED_MODES
    assert [u for u, _ in _run(sudo.sudo_users())] == [70]
    _run(sudo.revoke_sudo(70))
    assert _run(sudo.engine_mode(70, fresh=True)) == sudo.NONE


def test_five_referrals_activate_premium_sudo(fresh_db):
    assert ac.REFERRAL_REWARDS[0][0] == 5
    for referred in range(100, 104):
        _run(fresh_db.add_referral(80, referred))
    _run(ac.check_referral_milestones(80))
    assert _run(sudo.engine_mode(80, fresh=True)) == sudo.NONE   # 4 referrals: no
    _run(fresh_db.add_referral(80, 104))
    _run(ac.check_referral_milestones(80))
    assert _run(sudo.engine_mode(80, fresh=True)) == sudo.PREMIUM


def test_sudo_accounts_dedupe(fresh_db):
    s = "A" * 300
    assert _run(sudo.add_account(s, {"id": 9}))[1] == "added"
    assert _run(sudo.add_account(s, {"id": 9}))[1] == "updated"
    assert len(_run(sudo.accounts())) == 1
    assert _run(sudo.add_account("short"))[0] is False


def test_one_sudo_id_serves_one_group_at_a_time(fresh_db, monkeypatch):
    from types import SimpleNamespace
    sess = "B" * 300
    _run(sudo.add_account(sess, {"id": 9}))
    key = sudo.account_key(sess)
    playing = SimpleNamespace(is_playing=True, mic_enabled=False, ss_on=False)
    monkeypatch.setitem(sudo._engines, key, SimpleNamespace(chats={-1001: playing}))
    _run(sudo.save_gc(90, -1001, "", "G1", key))
    _run(sudo.save_gc(91, -1002, "", "G2", key))
    rows = _run(sudo.accounts())
    assert _run(sudo._assigned_key(90, rows)) == key        # same group: allowed
    assert _run(sudo._assigned_key(91, rows)) == sudo.BUSY  # other group: busy
    playing.is_playing = False
    assert _run(sudo._assigned_key(91, rows)) == key        # free again
