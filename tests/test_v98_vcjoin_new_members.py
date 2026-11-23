"""v98 VC JOINER: /setvc + Join All ab un accounts ko bhi VC me laata hai
jo target group ke member nahi the (pehle sirf group me added accounts
hi join hote the)."""
import asyncio
import types

import pytest
from pyrogram import errors

from plugins import vc_joiner as vj

CID = -1001234567890
LINK = "https://t.me/+abcDEF"


class FakeUserClient:
    def __init__(self, member=False, join_error=None):
        self.member = member
        self.join_error = join_error
        self.joined_refs = []

    async def get_chat_member(self, chat_id, who):
        if not self.member:
            raise errors.UserNotParticipant()
        return types.SimpleNamespace(status=types.SimpleNamespace(name="MEMBER"))

    async def join_chat(self, ref):
        self.joined_refs.append(ref)
        if self.join_error is not None:
            err, self.join_error = self.join_error, None
            raise err
        self.member = True

    async def resolve_peer(self, chat_id):
        if not self.member:
            raise errors.PeerIdInvalid()
        return object()

    async def get_chat(self, chat_id):
        if not self.member:
            raise errors.ChannelInvalid()
        return types.SimpleNamespace(id=chat_id, username=None, invite_link=None)


class FakeCalls:
    def __init__(self, client):
        self.client = client
        self.played = []

    async def play(self, chat_id, stream=None, *a):
        if not self.client.member:
            raise errors.ChannelInvalid()
        self.played.append(chat_id)

    async def mute(self, chat_id):
        return True

    @property
    async def calls(self):
        return {c: object() for c in self.played}


class FakeBot:
    def __init__(self, link=LINK):
        self.link = link
        self.approved = []

    async def get_chat(self, chat_id):
        return types.SimpleNamespace(id=chat_id, username=None, invite_link=self.link)

    async def approve_chat_join_request(self, chat_id, user_id):
        self.approved.append(user_id)
        return True


def make_uvc(uid, **kw):
    client = FakeUserClient(**kw)
    return types.SimpleNamespace(owner_id=uid, account_id=uid + 1000,
                                 account_username="", client=client,
                                 calls=FakeCalls(client))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    vj._joined.clear(); vj._failed_at.clear(); vj._last_reason.clear(); vj._member_refs.clear()
    monkeypatch.setattr(vj, "_save_state", lambda: None)
    monkeypatch.setattr(vj.asyncio, "sleep", _nosleep)
    yield
    vj._joined.clear()


_real_sleep = asyncio.sleep


async def _nosleep(*_a, **_k):
    await _real_sleep(0)


def run(coro):
    return asyncio.run(coro)


def test_non_member_joins_group_via_link_then_vc():
    uvc = make_uvc(1)
    target = {"chat_id": CID, "title": "t", "ref": LINK}
    assert run(vj._join_one(uvc, CID, bot=FakeBot(), target=target)) is True
    assert uvc.client.joined_refs == [LINK]
    assert uvc.calls.played == [CID]


def test_existing_member_does_not_rejoin_group():
    uvc = make_uvc(2, member=True)
    assert run(vj._join_one(uvc, CID, bot=FakeBot(), target={"chat_id": CID, "ref": LINK})) is True
    assert uvc.client.joined_refs == []
    assert uvc.calls.played == [CID]


def test_join_request_gets_approved_by_bot():
    uvc = make_uvc(3, join_error=errors.InviteRequestSent())
    bot = FakeBot()

    async def approve(chat_id, user_id):
        bot.approved.append(user_id)
        uvc.client.member = True
        return True
    bot.approve_chat_join_request = approve
    assert run(vj._join_one(uvc, CID, bot=bot, target={"chat_id": CID, "ref": LINK})) is True
    assert bot.approved == [1003]


def test_dead_link_is_refreshed_from_bot():
    uvc = make_uvc(4, join_error=errors.InviteHashExpired())
    target = {"chat_id": CID, "ref": "https://t.me/+old"}
    assert run(vj._join_one(uvc, CID, bot=FakeBot(), target=target)) is True
    assert uvc.client.joined_refs == ["https://t.me/+old", LINK]
    assert target["ref"] == LINK


def test_no_link_fails_with_clear_reason_without_playing():
    uvc = make_uvc(5)
    ok = run(vj._join_one(uvc, CID, bot=FakeBot(link=None), target={"chat_id": CID}))
    assert ok is False
    assert vj._last_reason[(5, CID)] == "no_link"
    assert uvc.calls.played == []


def test_join_all_brings_members_and_non_members(monkeypatch):
    users = {10: make_uvc(10, member=True), 11: make_uvc(11), 12: make_uvc(12)}
    monkeypatch.setattr(vj.session_manager, "users", users, raising=False)

    async def all_ids():
        return list(users)
    monkeypatch.setattr(vj, "_all_user_ids", all_ids)
    monkeypatch.setattr(vj, "_state", {"targets": [{"chat_id": CID, "title": "t"}], "auto": "0"})
    res = run(vj._join_all_target(CID, bot=FakeBot()))
    assert res["joined"] == 3
    assert res["failed"] == 0
    assert all(u.calls.played == [CID] for u in users.values())
