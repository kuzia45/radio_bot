import unittest
from unittest.mock import patch

from spotify import (
    SpotifyConfig,
    build_authorization_url,
    fetch_all_liked_tracks,
    fetch_recent_liked_tracks,
    normalize_track,
    split_radio_track,
    tracks_match,
)


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, **_: object) -> dict:
        return {
            "items": [
                {
                    "track": {
                        "name": "Uprising",
                        "artists": [{"name": "Muse"}],
                    }
                }
            ]
        }


class FakeSession:
    def get(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse()


class SpotifyPreparationTests(unittest.TestCase):
    def test_config_requires_all_credentials(self) -> None:
        self.assertFalse(SpotifyConfig("id", "", "callback").is_configured)
        self.assertTrue(SpotifyConfig("id", "secret", "callback").is_configured)

    @patch("spotify.secrets.token_urlsafe", return_value="nonce")
    def test_authorization_url_requests_library_access(self, _: object) -> None:
        config = SpotifyConfig("client id", "secret", "https://example.com/callback")
        url = build_authorization_url(config, 42)
        self.assertIn("client_id=client+id", url)
        self.assertIn("scope=user-library-read", url)
        self.assertIn("state=42%3Anonce", url)

    def test_normalization_handles_common_radio_variants(self) -> None:
        self.assertEqual(normalize_track("Ёлка & A (Radio Edit)"), "елка and a")

    def test_track_matching(self) -> None:
        self.assertTrue(tracks_match("Muse - Uprising", "MUSE", "Uprising"))
        self.assertFalse(tracks_match("Muse - Uprising", "Muse", "Starlight"))
        self.assertIsNone(split_radio_track("Unknown title"))

    def test_fuzzy_track_matching_allows_small_typos(self) -> None:
        self.assertTrue(
            tracks_match(
                "Calvin Haris - How Deep Is Your Loue",
                "Calvin Harris",
                "How Deep Is Your Love",
            )
        )

    def test_fuzzy_track_matching_rejects_different_tracks(self) -> None:
        self.assertFalse(tracks_match("Muse - Uprising", "Muse", "Starlight"))
        self.assertFalse(tracks_match("Artist - Love", "Artist", "Lover"))
        self.assertFalse(
            tracks_match("Coldplay - Yellow", "Coldplay", "Yellow Submarine")
        )


class SpotifyApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_liked_tracks_are_parsed(self) -> None:
        tracks = await fetch_recent_liked_tracks(FakeSession(), "token")  # type: ignore[arg-type]
        self.assertEqual(
            [(track.artist, track.title) for track in tracks], [("Muse", "Uprising")]
        )

    async def test_all_liked_tracks_are_parsed(self) -> None:
        tracks = await fetch_all_liked_tracks(FakeSession(), "token")  # type: ignore[arg-type]
        self.assertEqual(
            [(track.artist, track.title) for track in tracks], [("Muse", "Uprising")]
        )


if __name__ == "__main__":
    unittest.main()
