"""Поиск радиопотоков и чтение ICY-метаданных текущего трека."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

RADIO_BROWSER_URL = "https://all.api.radio-browser.info/json/stations/search"
USER_AGENT = "RadioNowTelegramBot/1.0"


@dataclass(frozen=True, slots=True)
class Station:
    key: str
    title: str
    search_name: str
    aliases: tuple[str, ...]


STATIONS: tuple[Station, ...] = (
    Station("europa", "Европа Плюс", "Европа Плюс", ("европа плюс", "europa plus")),
    Station(
        "rusradio", "Русское Радио", "Русское Радио", ("русское радио", "russian radio")
    ),
    Station("avtoradio", "Авторадио", "Авторадио", ("авторадио", "avtoradio")),
    Station(
        "energy",
        "Радио ENERGY",
        "NRJ Russia",
        ("nrj russia", "energy russia", "радио energy"),
    ),
    Station(
        "maximum",
        "Радио MAXIMUM",
        "Radio Maximum",
        ("radio maximum", "радио maximum", "maximum"),
    ),
    Station("love", "Love Radio", "Love Radio", ("love radio", "лав радио")),
    Station("hitfm", "Хит FM", "Хит FM", ("хит fm", "hit fm")),
    Station("doroga", "Дорожное радио", "Дорожное радио", ("дорожное радио",)),
    Station("retro", "Ретро FM", "Ретро FM", ("ретро fm", "retro fm")),
    Station("record", "Радио Рекорд", "Radio Record", ("radio record", "радио рекорд")),
    Station("like", "Like FM", "Like FM", ("like fm",)),
    Station("dfm", "DFM", "DFM", ("dfm", "динамит fm")),
    Station("nashe", "Наше Радио", "Наше Радио", ("наше радио", "nashe radio")),
    Station("comedy", "Comedy Radio", "Comedy Radio", ("comedy radio",)),
    Station(
        "shanson", "Радио Шансон", "Радио Шансон", ("радио шансон", "radio shanson")
    ),
    Station(
        "montecarlo",
        "Радио Monte Carlo",
        "Monte Carlo Russia",
        ("monte carlo russia", "радио monte carlo"),
    ),
    Station(
        "silver",
        "Серебряный Дождь",
        "Серебряный Дождь",
        ("серебряный дождь", "silver rain"),
    ),
    Station(
        "business",
        "Business FM",
        "Business FM Moscow",
        ("business fm moscow", "business fm"),
    ),
    Station("mayak", "Радио Маяк", "Радио Маяк", ("радио маяк", "radio mayak")),
    Station("vesti", "Вести FM", "Вести FM", ("вести fm", "vesti fm")),
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


class RadioService:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def now_playing(self, station: Station) -> str | None:
        source = await self._find_stream(station)
        return await self._read_stream_title(source["url_resolved"] or source["url"])

    async def _find_stream(self, station: Station) -> dict[str, Any]:
        cached = self._cache.get(station.key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        params = {
            "name": station.search_name,
            "countrycode": "RU",
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": "30",
        }
        try:
            async with self.session.get(RADIO_BROWSER_URL, params=params) as response:
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

        selected = max(candidates, key=score)
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

                await response.content.readexactly(interval)
                length = (await response.content.readexactly(1))[0] * 16
                if length == 0:
                    return None
                return parse_icy_metadata(await response.content.readexactly(length))
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ValueError,
        ) as exc:
            raise RadioError("Аудиопоток станции сейчас недоступен") from exc
