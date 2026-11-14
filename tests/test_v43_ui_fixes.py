"""Tests for v43 screen-share user picture and mic background blur fixes."""
import pytest
from unittest.mock import AsyncMock, MagicMock

class MockChatPhoto:
    def __init__(self, big='big_fid_123', small='small_fid_456'):
        self.big_file_id = big
        self.small_file_id = small

class MockUser:
    def __init__(self, uid=1001, photo=None):
        self.id = uid
        self.photo = photo or MockChatPhoto()
        self.first_name = 'Test'
        self.last_name = 'User'
        self.username = 'testuser'

class MockPhoto:
    def __init__(self, fid='photo_fid_789'):
        self.file_id = fid
        self.big_file_id = fid

async def mock_get_chat_photos(uid, limit=1):
    yield MockPhoto('photo_fid_789')

def test_ss_avatar_async_generator():
    import asyncio
    async def _test():
        from helpers.vc_manager import UserVC
        uvc = UserVC(1001, 'dummy_session')
        client = MagicMock()
        client.get_users = AsyncMock(return_value=None)
        client.get_chat_photos = mock_get_chat_photos
        async def fake_download(fid, file_name):
            return file_name
        client.download_media = AsyncMock(side_effect=fake_download)
        uvc.client = client
        # Ensures async generator is consumed without TypeError
        path = await uvc._ss_avatar(1001)
        assert client.download_media.called
    asyncio.run(_test())

def test_ss_avatar_direct_user_photo():
    import asyncio
    async def _test():
        from helpers.vc_manager import UserVC
        uvc = UserVC(1001, 'dummy_session')
        client = MagicMock()
        client.get_users = AsyncMock(return_value=MockUser(photo=MockChatPhoto('my_big_fid')))
        async def fake_download(fid, file_name):
            return file_name
        client.download_media = AsyncMock(side_effect=fake_download)
        uvc.client = client
        path = await uvc._ss_avatar(1001)
        assert client.download_media.called
        assert client.download_media.call_args[0][0] == 'my_big_fid'
    asyncio.run(_test())

def test_telegram_live_screen_render():
    from helpers import telegram_live_screen
    rnd = telegram_live_screen.Renderer(1280, 720)
    st = {
        'me_name': 'Warrior',
        'me_user': 'warrior',
        'me_photo': '',
        'mic_on': True,
        'audio_active': True,
        'participants': [{'uid': 1001, 'name': 'Warrior', 'photo': '', 'is_me': True, 'speaking': True}]
    }
    img = rnd.render(st, 1.0, 0.0)
    assert img.size == (1280, 720)

def test_live_mic_html_blur_elements():
    from helpers.live_mic import LIVE_MIC_HTML
    assert 'mic-blur-bg' in LIVE_MIC_HTML
    assert 'ambient-mic-blur' in LIVE_MIC_HTML
    assert 'orb-glow' in LIVE_MIC_HTML
