import unittest

from src.bot import should_log_airplay


class AirplayLoggingTests(unittest.TestCase):
    def test_duplicate_is_suppressed_for_three_minutes(self) -> None:
        state: dict[str, tuple[str, float]] = {}
        self.assertTrue(should_log_airplay(state, "maximum", "Muse - Uprising", 0))
        self.assertFalse(should_log_airplay(state, "maximum", "Muse - Uprising", 179))
        self.assertTrue(should_log_airplay(state, "maximum", "Muse - Uprising", 180))

    def test_new_track_is_logged_immediately(self) -> None:
        state = {"maximum": ("Muse - Uprising", 100)}
        self.assertTrue(should_log_airplay(state, "maximum", "Muse - Starlight", 101))


if __name__ == "__main__":
    unittest.main()
