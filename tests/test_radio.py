import unittest

from src.radio import STATIONS_BY_KEY, parse_icy_metadata, parse_official_page


class ParseIcyMetadataTests(unittest.TestCase):
    def test_requested_stations_are_available(self) -> None:
        self.assertTrue({"maximum", "love", "hitfm"} <= STATIONS_BY_KEY.keys())

    def test_station_order(self) -> None:
        self.assertEqual(
            list(STATIONS_BY_KEY),
            [
                "maximum",
                "like",
                "comedy",
                "energy",
                "love",
                "europa",
                "dfm",
                "record",
                "kissfm",
                "hitfm",
                "montecarlo",
            ],
        )

    def test_station_json_page(self) -> None:
        page = r"\"slug\":\"maximum\",\"current\":{\"artist\":\"Muse\",\"title\":\"Uprising\"}"
        self.assertEqual(
            parse_official_page(page, "station_json", "maximum"), "Muse - Uprising"
        )

    def test_online_radio_box_page(self) -> None:
        page = """
        <tr class="active">
          <td class="tablelist-schedule__time">сейчас</td>
          <td class="track_history_item">
            <a href="/track/1/">A &amp; B - Song</a>
          </td>
        </tr>
        """
        self.assertEqual(parse_official_page(page, "online_radio_box"), "A & B - Song")

    def test_energy_page(self) -> None:
        page = "Image: Обложка трека 'Song - Artist'"
        self.assertEqual(parse_official_page(page, "energy"), "Artist - Song")

    def test_like_playlist(self) -> None:
        page = '<button data-track-artist="A &amp; B" data-track-title="Song">'
        self.assertEqual(parse_official_page(page, "like_playlist"), "A & B - Song")

    def test_comedy_playlist(self) -> None:
        page = '<button data-track-author="Artist" data-track-title="Track">'
        self.assertEqual(parse_official_page(page, "comedy_playlist"), "Artist - Track")

    def test_europa_playlist(self) -> None:
        page = (
            '<div class="track-and-artists">'
            '<h4 class="track-name">Song</h4>'
            '<h4 class="artist">A &amp; B</h4></div>'
        )
        self.assertEqual(parse_official_page(page, "europa_playlist"), "A & B - Song")

    def test_volna_top(self) -> None:
        page = "<p class='supertext'>Сейчас играет: A &amp; B - New Song</p>"
        self.assertEqual(parse_official_page(page, "volna_top"), "A & B - New Song")

    def test_json_title(self) -> None:
        page = '{"ok": true, "title": "Artist  -  Song"}'
        self.assertEqual(parse_official_page(page, "json_title"), "Artist - Song")

    def test_hls_metadata(self) -> None:
        page = '#EXT-X-DATERANGE:X-ARTIST="A &amp; B",X-TITLE="New Song"'
        self.assertEqual(parse_official_page(page, "hls_metadata"), "A & B - New Song")

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
