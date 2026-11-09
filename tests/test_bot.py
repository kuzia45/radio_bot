import unittest
from datetime import datetime, timedelta

from src.bot import MOSCOW_TZ, is_quiet_time, should_log_airplay, station_health_state


class AirplayLoggingTests(unittest.TestCase):
    def test_duplicate_is_suppressed_for_three_minutes(self) -> None:
        state: dict[str, tuple[str, float]] = {}
        self.assertTrue(should_log_airplay(state, "maximum", "Muse - Uprising", 0))
        self.assertFalse(should_log_airplay(state, "maximum", "Muse - Uprising", 179))
        self.assertTrue(should_log_airplay(state, "maximum", "Muse - Uprising", 180))

    def test_new_track_is_logged_immediately(self) -> None:
        state = {"maximum": ("Muse - Uprising", 100)}
        self.assertTrue(should_log_airplay(state, "maximum", "Muse - Starlight", 101))

    def test_quiet_hours_can_cross_midnight(self) -> None:
        self.assertTrue(is_quiet_time(23, (23, 8)))
        self.assertTrue(is_quiet_time(7, (23, 8)))
        self.assertFalse(is_quiet_time(12, (23, 8)))
        self.assertFalse(is_quiet_time(23, None))

    def test_station_health_detects_failures_and_stale_track(self) -> None:
        now = datetime(2026, 11, 9, 18, 0, tzinfo=MOSCOW_TZ)
        healthy = ("maximum", "Muse - Uprising", None, now.isoformat(), 0, None)
        stale = (
            "maximum",
            "Muse - Uprising",
            None,
            (now - timedelta(minutes=31)).isoformat(),
            0,
            None,
        )
        unavailable = ("maximum", None, None, None, 3, "timeout")

        self.assertEqual(station_health_state(healthy, now), "healthy")
        self.assertEqual(station_health_state(stale, now), "stale")
        self.assertEqual(station_health_state(unavailable, now), "unavailable")


if __name__ == "__main__":
    unittest.main()
