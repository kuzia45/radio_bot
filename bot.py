"""Telegram-бот, показывающий текущие треки российских радиостанций."""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from radio import STATIONS, STATIONS_BY_KEY, RadioError, RadioService, Station

router = Router()
radio_service: RadioService | None = None
STATIONS_PER_PAGE = 8


def stations_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    page_count = (len(STATIONS) + STATIONS_PER_PAGE - 1) // STATIONS_PER_PAGE
    page = max(0, min(page, page_count - 1))
    start = page * STATIONS_PER_PAGE
    stations = STATIONS[start : start + STATIONS_PER_PAGE]
    rows = []
    for index in range(0, len(stations), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=station.title, callback_data=f"station:{station.key}"
                )
                for station in stations[index : index + 2]
            ]
        )
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="◀️", callback_data=f"stations:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="▶️", callback_data=f"stations:{page + 1}")
        )
    rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_keyboard(station: Station) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data=f"station:{station.key}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📻 Другие станции", callback_data="stations:0"
                )
            ],
        ]
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "🎵 <b>Что сейчас играет?</b>\n\nВыберите радиостанцию:",
        reply_markup=stations_keyboard(),
    )


@router.callback_query(F.data.startswith("stations:"))
async def show_stations(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        page = int((callback.data or "stations:0").partition(":")[2])
    except ValueError:
        page = 0
    if callback.message:
        await callback.message.edit_text(
            "🎵 <b>Что сейчас играет?</b>\n\nВыберите радиостанцию:",
            reply_markup=stations_keyboard(page),
        )


@router.callback_query(F.data == "noop")
async def ignore_page_counter(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("station:"))
async def show_track(callback: CallbackQuery) -> None:
    await callback.answer("Проверяю эфир…")
    key = (callback.data or "").partition(":")[2]
    station = STATIONS_BY_KEY.get(key)
    if station is None or callback.message is None:
        return

    assert radio_service is not None
    try:
        title = await radio_service.now_playing(station)
        if title:
            body = f"🎙 <b>{html.escape(station.title)}</b>\n\nСейчас играет:\n🎵 <b>{html.escape(title)}</b>"
        else:
            body = (
                f"🎙 <b>{html.escape(station.title)}</b>\n\n"
                "Станция сейчас не передаёт название трека в эфирном потоке."
            )
    except RadioError as exc:
        body = f"🎙 <b>{html.escape(station.title)}</b>\n\n⚠️ {html.escape(str(exc))}. Попробуйте ещё раз."

    checked_at = datetime.now().astimezone().strftime("%H:%M:%S")
    await callback.message.edit_text(
        f"{body}\n\n<i>Проверено в {checked_at}</i>",
        reply_markup=result_keyboard(station),
    )


async def main() -> None:
    global radio_service
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен."
        )

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    timeout = aiohttp.ClientTimeout(total=15, connect=6, sock_read=10)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": "RadioNowTelegramBot/1.0"}
    ) as session:
        radio_service = RadioService(session)
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        try:
            await dispatcher.start_polling(bot)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
