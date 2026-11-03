"""v76: a self-set 200 % by a NON-admin is only a local slider - it must not
count as confirmed (listeners still hear 100 %)."""
import asyncio, os, sys, types
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
vm = pytest.importorskip("helpers.vc_manager")


def _o(rows):
    o = types.SimpleNamespace()

    async def parts(cid):
        return rows
    o.vc_participants = parts
    return o


def test_non_admin_200_reads_as_100():
    o = _o([{"uid": 5, "volume": 200, "by_admin": False}])
    assert asyncio.run(vm.UserVC.read_participant_volume(o, 1, 5)) == 10000


def test_admin_200_reads_as_200():
    o = _o([{"uid": 5, "volume": 200, "by_admin": True}])
    assert asyncio.run(vm.UserVC.read_participant_volume(o, 1, 5)) == 20000
