"""Проверка получения текущего трека для всех настроенных станций."""

from __future__ import annotations

import asyncio

import aiohttp

from src.radio import STATIONS, RadioError, RadioService


async def check_station(service: RadioService, key: str, title: str) -> tuple[str, str]:
    station = next(item for item in STATIONS if item.key == key)
    try:
        track = await service.now_playing(station)
    except RadioError as exc:
        return title, f"ERROR: {exc}"
    return title, track or "NO METADATA"


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        service = RadioService(session)
        for station in STATIONS:
            title, result = await check_station(service, station.key, station.title)
            print(f"{title}: {result}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
