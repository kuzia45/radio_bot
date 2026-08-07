import tempfile
import unittest
from pathlib import Path

from src.spotify_store import SpotifyStore, StoredSpotifyToken


class SpotifyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "spotify.sqlite3"
        self.store = SpotifyStore(path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_state_can_only_be_consumed_once(self) -> None:
        self.store.save_state("state", 42)
        self.assertEqual(self.store.consume_state("state"), 42)
        self.assertIsNone(self.store.consume_state("state"))

    def test_token_is_persisted(self) -> None:
        token = StoredSpotifyToken("access", "refresh", 123)
        self.store.save_token(42, token)
        self.assertEqual(self.store.get_token(42), token)
        self.assertEqual(self.store.connected_user_ids(), [42])

    def test_same_station_track_is_not_notified_twice(self) -> None:
        self.assertTrue(self.store.mark_notification(42, "maximum", "Muse - Uprising"))
        self.assertFalse(self.store.mark_notification(42, "maximum", "Muse - Uprising"))
        self.assertTrue(self.store.mark_notification(42, "maximum", "Muse - Starlight"))


if __name__ == "__main__":
    unittest.main()
