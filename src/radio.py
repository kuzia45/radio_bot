"""Поиск радиопотоков и чтение ICY-метаданных текущего трека."""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp

RADIO_BROWSER_URL = "https://all.api.radio-browser.info/json/stations/search"
USER_AGENT = "RadioNowTelegramBot/1.0"


@dataclass(frozen=True, slots=True)
class Station:
    key: str
    title: str
    search_name: str
    aliases: tuple[str, ...]
    official_url: str | None = None
    official_parser: str | None = None
    official_slug: str | None = None
    country_code: str = "RU"


STATIONS: tuple[Station, ...] = (
    Station(
        "maximum",
        "Максимум",
        "Radio Maximum",
        ("radio maximum", "радио maximum", "maximum"),
        "https://maximum.ru/online/",
        "station_json",
        "maximum",
    ),
    Station(
        "like",
        "Лайк ФМ",
        "Like FM",
        ("like fm", "лайк фм"),
        "https://www.likefm.ru/playlist",
        "like_playlist",
    ),
    Station(
        "comedy",
        "Камеди Радио",
        "Comedy Radio",
        ("comedy radio", "камеди радио"),
        "https://comedy-radio.ru/playlist",
        "comedy_playlist",
    ),
    Station(
        "energy",
        "Энерджи",
        "NRJ Russia",
        ("nrj russia", "energy russia", "радио energy"),
        "https://hls-01-gpm.hostingradio.ru/energyfm495/128/playlist.m3u8",
        "hls_metadata",
    ),
    Station(
        "love",
        "Лав Радио",
        "Love Radio",
        ("love radio", "лав радио"),
        "https://onlineradiobox.com/kz/loveradio/playlist/?lang=ru",
        "online_radio_box",
    ),
    Station(
        "europa",
        "Европа Плюс",
        "Европа Плюс",
        ("европа плюс", "europa plus"),
        "https://fmgo.net/api/nowplaying?slug=evropa-plyus",
        "json_title",
    ),
    Station("dfm", "ДФМ", "DFM", ("dfm", "дфм", "динамит fm")),
    Station("record", "Рекорд", "Radio Record", ("radio record", "радио рекорд")),
    Station(
        "kissfm",
        "Кисс ФМ",
        "KISS FM Ukraine",
        ("kiss fm ukraine", "kiss fm", "кисс фм"),
        country_code="UA",
    ),
    Station(
        "hitfm",
        "Хит FM",
        "Хит FM",
        ("хит fm", "hit fm"),
        "https://hitfm.ru/",
        "station_json",
        "hit-fm",
    ),
    Station(
        "montecarlo",
        "Монте Карло",
        "Radio Monte Carlo Russia",
        ("radio monte carlo russia", "monte carlo russia", "радио monte carlo"),
        "https://montecarlo.ru/",
        "station_json",
        "radio-monte-carlo",
    ),
)

STATIONS_BY_KEY = {station.key: station for station in STATIONS}


class RadioError(RuntimeError):
    """Поток или каталог радиостанций недоступен."""


def parse_icy_metadata(block: bytes) -> str | None:
    """Извлекает StreamTitle из ICY-блока, учитывая русские кодировки."""
    raw = block.rstrip(b"\0")
    if not raw:
        return None

    text = ""
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    match = re.search(r"StreamTitle='(.*?)';", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r'StreamTitle="(.*?)";', text, flags=re.IGNORECASE)
    if not match:
        return None

    title = re.sub(r"\s+", " ", match.group(1)).strip(" \t-–—")
    return title or None


def parse_official_page(page: str, parser: str, slug: str | None = None) -> str | None:
    """Извлекает эфирный трек из серверной разметки официального сайта."""
    if parser == "station_json" and slug:
        pattern = rf'\\"slug\\":\\"{re.escape(slug)}\\".*?\\"current\\":\{{(.*?)\}}'
        current = re.search(pattern, page, flags=re.DOTALL)
        if not current:
            return None
        artist = re.search(r'\\"artist\\":\\"(.*?)\\"', current.group(1))
        title = re.search(r'\\"title\\":\\"(.*?)\\"', current.group(1))
        if artist and title:
            return f"{json.loads(f'"{artist.group(1)}"')} - {json.loads(f'"{title.group(1)}"')}"

    if parser == "json_title":
        try:
            payload = json.loads(page)
        except (TypeError, ValueError):
            return None
        title = payload.get("title")
        if payload.get("ok") is True and isinstance(title, str):
            return re.sub(r"\s+", " ", title).strip() or None

    if parser == "hls_metadata":
        artist = re.search(r'X-ARTIST="([^"]+)"', page)
        title = re.search(r'X-TITLE="([^"]+)"', page)
        if artist and title:
            return f"{html.unescape(artist.group(1))} - {html.unescape(title.group(1))}"

    if parser == "online_radio_box":
        current = re.search(
            r'<tr\s+class="active".*?class="track_history_item"[^>]*>\s*'
            r"<a[^>]*>(.*?)</a>",
            page,
            re.DOTALL,
        )
        if current:
            title = re.sub(r"<[^>]+>", "", current.group(1))
            return re.sub(r"\s+", " ", html.unescape(title)).strip() or None

    if parser == "energy":
        latest = re.search(r"Обложка трека '([^']+) - ([^']+)'", page)
        if latest:
            return (
                f"{html.unescape(latest.group(2))} - {html.unescape(latest.group(1))}"
            )

    if parser == "like_playlist":
        latest = re.search(
            r'data-track-artist="([^"]+)"[^>]*data-track-title="([^"]+)"', page
        )
        if latest:
            return (
                f"{html.unescape(latest.group(1))} - {html.unescape(latest.group(2))}"
            )

    if parser == "comedy_playlist":
        latest = re.search(
            r'data-track-author="([^"]+)"[^>]*data-track-title="([^"]+)"', page
        )
        if latest:
            return (
                f"{html.unescape(latest.group(1))} - {html.unescape(latest.group(2))}"
            )

    if parser == "europa_playlist":
        latest = re.search(
            r'class="track-and-artists"[^>]*>\s*'
            r'<h4 class="track-name"[^>]*>(.*?)</h4>\s*'
            r'<h4 class="artist"[^>]*>(.*?)</h4>',
            page,
            re.DOTALL,
        )
        if latest:
            return f"{html.unescape(latest.group(2)).strip()} - {html.unescape(latest.group(1)).strip()}"
    return None


class RadioService:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def now_playing(self, station: Station) -> str | None:
        if (
            station.official_url
            and station.official_parser
            and (title := await self._read_official_page(station))
        ):
            return title

        sources = await self._find_streams(station)
        tasks = [
            asyncio.create_task(
                self._read_stream_title(source["url_resolved"] or source["url"])
            )
            for source in sources
        ]
        errors: list[RadioError] = []
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    if title := await task:
                        return title
                except RadioError as exc:
                    errors.append(exc)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        if len(errors) == len(tasks) and errors:
            raise errors[0]
        return None

    async def _read_official_page(self, station: Station) -> str | None:
        browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/127.0 Safari/537.36"
            )
        }
        try:
            async with self.session.get(
                station.official_url,
                headers=browser_headers,
            ) as response:
                response.raise_for_status()
                page = await response.text(errors="replace")
                if station.official_parser == "hls_metadata":
                    variant = next(
                        (
                            line.strip()
                            for line in page.splitlines()
                            if line.strip() and not line.startswith("#")
                        ),
                        None,
                    )
                    if not variant:
                        return None
                    variant_url = urljoin(str(response.url), variant)
                    async with self.session.get(
                        variant_url,
                        headers=browser_headers,
                    ) as variant_response:
                        variant_response.raise_for_status()
                        page = await variant_response.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return parse_official_page(
            page, station.official_parser or "", station.official_slug
        )

    async def _find_streams(self, station: Station) -> list[dict[str, Any]]:
        cached = self._cache.get(station.key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        params = {
            "name": station.search_name,
            "countrycode": station.country_code,
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": "30",
        }
        try:
            async with self.session.get(
                RADIO_BROWSER_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                response.raise_for_status()
                results = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise RadioError("Каталог радиостанций временно недоступен") from exc

        candidates = [
            item for item in results if item.get("url_resolved") or item.get("url")
        ]
        if not candidates:
            raise RadioError("Не удалось найти рабочий поток станции")

        def score(item: dict[str, Any]) -> tuple[int, int, int]:
            name = str(item.get("name", "")).casefold().strip()
            exact = int(name in station.aliases)
            alias_hit = int(any(alias in name for alias in station.aliases))
            return exact, alias_hit, int(item.get("votes") or 0)

        selected = sorted(candidates, key=score, reverse=True)[:5]
        self._cache[station.key] = (time.monotonic() + 15 * 60, selected)
        return selected

    async def _read_stream_title(self, url: str) -> str | None:
        headers = {"Icy-MetaData": "1", "User-Agent": USER_AGENT}
        try:
            async with self.session.get(
                url, headers=headers, allow_redirects=True
            ) as response:
                response.raise_for_status()
                interval_value = response.headers.get("icy-metaint")
                if not interval_value:
                    return None
                interval = int(interval_value)
                if interval <= 0 or interval > 2_000_000:
                    return None

                for _ in range(5):
                    await response.content.readexactly(interval)
                    length = (await response.content.readexactly(1))[0] * 16
                    if length == 0:
                        continue
                    metadata = await response.content.readexactly(length)
                    if title := parse_icy_metadata(metadata):
                        return title
                return None
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ValueError,
        ) as exc:
            raise RadioError("Аудиопоток станции сейчас недоступен") from exc
