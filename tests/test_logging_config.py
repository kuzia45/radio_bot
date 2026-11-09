import logging
import unittest

from src.logging_config import MoscowConsoleFormatter


class LoggingConfigTests(unittest.TestCase):
    def test_console_log_is_readable_and_uses_moscow_time(self) -> None:
        record = logging.LogRecord(
            name="src.bot",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Bot ready",
            args=(),
            exc_info=None,
        )
        record.created = 0
        formatter = MoscowConsoleFormatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
        )
        rendered = formatter.format(record)
        self.assertEqual(
            rendered, "01.01.1970 03:00:00 | INFO    | src.bot | Bot ready"
        )


if __name__ == "__main__":
    unittest.main()
