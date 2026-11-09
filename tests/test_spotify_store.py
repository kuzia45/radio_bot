import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.spotify_store import SpotifyStore, StoredSpotifyToken, StoredYandexToken


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

    def test_yandex_token_is_persisted(self) -> None:
        token = StoredYandexToken("access", "refresh", 123)
        self.store.save_yandex_token(43, token)
        self.assertEqual(self.store.get_yandex_token(43), token)
        self.assertEqual(self.store.music_user_ids(), [43])

    def test_same_station_track_is_not_notified_twice(self) -> None:
        self.assertTrue(self.store.mark_notification(42, "maximum", "Muse - Uprising"))
        self.assertFalse(self.store.mark_notification(42, "maximum", "Muse - Uprising"))
        self.assertTrue(self.store.mark_notification(42, "maximum", "Muse - Starlight"))
        row = self.store.connection.execute(
            "SELECT radio_track, notified_at FROM spotify_notifications "
            "WHERE telegram_user_id = 42 AND station_key = 'maximum'"
        ).fetchone()
        self.assertEqual(row[0], "Muse - Starlight")
        self.assertRegex(
            row[1], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$"
        )

    def test_airplay_history_skips_consecutive_duplicates(self) -> None:
        self.assertTrue(self.store.record_airplay("maximum", "Muse - Uprising"))
        self.assertFalse(self.store.record_airplay("maximum", "Muse - Uprising"))
        self.assertTrue(self.store.record_airplay("maximum", "Muse - Starlight"))
        tracks = self.store.recent_airplay("maximum")
        self.assertEqual(
            [item[0] for item in tracks], ["Muse - Starlight", "Muse - Uprising"]
        )

    def test_favorites_and_notification_station_filter(self) -> None:
        self.assertTrue(self.store.toggle_favorite(42, "maximum"))
        self.assertEqual(self.store.favorite_station_keys(42), {"maximum"})
        self.assertFalse(self.store.toggle_favorite(42, "maximum"))

        all_keys = {"maximum", "love"}
        self.assertEqual(self.store.notification_station_keys(42, all_keys), all_keys)
        self.assertFalse(self.store.toggle_notification_station(42, "love", all_keys))
        self.assertEqual(
            self.store.notification_station_keys(42, all_keys), {"maximum"}
        )

    def test_quiet_hours_and_daily_matches(self) -> None:
        self.store.set_quiet_hours(42, (23, 8))
        self.assertEqual(self.store.quiet_hours(42), (23, 8))
        self.store.record_match(42, "maximum", "Muse - Uprising", delivered=False)
        date = datetime.now().astimezone().date().isoformat()
        self.assertEqual(len(self.store.daily_matches(42, date)), 1)
        self.assertTrue(self.store.digest_due(42, date, 20))
        self.store.mark_digest_sent(42, date)
        self.assertFalse(self.store.digest_due(42, date, 20))

    def test_station_health_only_reports_transitions(self) -> None:
        self.assertEqual(self.store.set_station_health("maximum", "healthy"), "unknown")
        self.assertIsNone(self.store.set_station_health("maximum", "healthy"))
        self.assertEqual(self.store.set_station_health("maximum", "stale"), "healthy")
        self.assertIsNone(self.store.set_station_health("maximum", "stale"))
        self.assertEqual(self.store.set_station_health("maximum", "healthy"), "stale")

    def test_spotify_library_is_synchronized_once_per_day(self) -> None:
        self.assertTrue(self.store.spotify_sync_due(42, "2026-11-09"))
        self.store.mark_spotify_sync_attempt(42, "2026-11-09")
        self.assertFalse(self.store.spotify_sync_due(42, "2026-11-09"))
        self.assertTrue(self.store.spotify_sync_due(42, "2026-11-10"))
        self.store.reset_spotify_sync_attempt(42)
        self.assertTrue(self.store.spotify_sync_due(42, "2026-11-09"))
        self.store.mark_spotify_sync_attempt(42, "2026-11-09")

        self.store.save_spotify_liked_tracks(
            42,
            [("Muse", "Uprising"), ("Muse", "Starlight")],
        )
        self.assertEqual(
            self.store.cached_spotify_liked_tracks(42, limit=1),
            [("Muse", "Uprising")],
        )
        status = self.store.spotify_sync_status(42)
        self.assertIsNotNone(status)
        self.assertIsNotNone(status[1])
        self.assertIsNone(status[2])

        self.store.save_spotify_sync_error(42, "quota")
        self.assertEqual(self.store.spotify_sync_status(42)[2], "quota")

    def test_personal_notification_rules(self) -> None:
        self.assertEqual(self.store.notification_rule(42), (None, 7))
        self.store.set_artist_filter(42, "Muse")
        self.store.set_repeat_days(42, 14)
        self.assertEqual(self.store.notification_rule(42), ("Muse", 14))


if __name__ == "__main__":
    unittest.main()
