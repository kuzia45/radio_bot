import unittest

from src.bot import is_quiet_time, should_log_airplay


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


if __name__ == "__main__":
    unittest.main()
