import unittest

from radio import STATIONS_BY_KEY, parse_icy_metadata


class ParseIcyMetadataTests(unittest.TestCase):
    def test_requested_stations_are_available(self) -> None:
        self.assertTrue({"maximum", "love", "hitfm"} <= STATIONS_BY_KEY.keys())

    def test_utf8_title(self) -> None:
        block = (
            "StreamTitle='Моя Мишель - Зима в сердце';StreamUrl='';".encode()
            + b"\0" * 10
        )
        self.assertEqual(parse_icy_metadata(block), "Моя Мишель - Зима в сердце")

    def test_cp1251_title(self) -> None:
        block = "StreamTitle='Кино - Звезда';".encode("cp1251")
        self.assertEqual(parse_icy_metadata(block), "Кино - Звезда")

    def test_empty_title(self) -> None:
        self.assertIsNone(parse_icy_metadata(b"StreamTitle='';\0\0"))

    def test_missing_title(self) -> None:
        self.assertIsNone(parse_icy_metadata(b"StreamUrl='https://example.com';"))


if __name__ == "__main__":
    unittest.main()
