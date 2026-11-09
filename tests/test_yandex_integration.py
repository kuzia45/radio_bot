import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.yandex_integration import (
    authorize_yandex_music,
    fetch_yandex_liked_tracks,
)


class FakeYandexClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    async def device_auth(self, on_code):
        code = SimpleNamespace(
            verification_url="https://ya.ru/device", user_code="ABCD", expires_in=600
        )
        await on_code(code)
        return SimpleNamespace(
            access_token="access", refresh_token="refresh", expires_in=3600
        )

    async def init(self):
        return self

    async def users_likes_tracks(self):
        return [SimpleNamespace(id="1", album_id="2")]

    async def tracks(self, identifiers):
        return [
            SimpleNamespace(
                id="1",
                title="Song",
                artists=[SimpleNamespace(name="Artist")],
            )
        ]


class YandexMusicTests(unittest.IsolatedAsyncioTestCase):
    @patch("src.yandex_integration.ClientAsync", FakeYandexClient)
    async def test_device_authorization_returns_tokens(self) -> None:
        codes = []

        async def capture_code(code) -> None:
            codes.append(code.user_code)

        token = await authorize_yandex_music(capture_code)
        self.assertEqual(token, ("access", "refresh", 3600))
        self.assertEqual(codes, ["ABCD"])

    @patch("src.yandex_integration.ClientAsync", FakeYandexClient)
    async def test_liked_tracks_are_converted_for_matching(self) -> None:
        tracks = await fetch_yandex_liked_tracks("access", limit=10)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].artist, "Artist")
        self.assertEqual(tracks[0].title, "Song")


if __name__ == "__main__":
    unittest.main()
