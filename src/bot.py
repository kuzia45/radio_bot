"""Telegram-бот, показывающий текущие треки российских радиостанций."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
)
from aiohttp import web
from dotenv import load_dotenv

from src.radio import STATIONS, STATIONS_BY_KEY, RadioError, RadioService, Station
from src.spotify import (
    SpotifyConfig,
    SpotifyError,
    create_authorization_url,
    exchange_authorization_code,
    fetch_all_liked_tracks,
    fetch_recent_liked_tracks,
    refresh_access_token,
    tracks_match,
)
from src.spotify_store import SpotifyStore, StoredSpotifyToken

router = Router()
logger = logging.getLogger(__name__)
airplay_logger = logging.getLogger("airplay")
radio_service: RadioService | None = None
spotify_config: SpotifyConfig | None = None
spotify_store: SpotifyStore | None = None
STATIONS_PER_PAGE = 8


def configure_airplay_logging() -> None:
    """Write radio history to a bounded, dedicated UTF-8 log file."""
    Path("logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        "logs/airplay.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    airplay_logger.addHandler(handler)
    airplay_logger.setLevel(logging.INFO)
    airplay_logger.propagate = False


def should_log_airplay(
    state: dict[str, tuple[str, float]],
    station_key: str,
    track: str,
    now: float,
) -> bool:
    """Suppress the same station/track pair for three minutes."""
    previous = state.get(station_key)
    if previous and previous[0] == track and now - previous[1] < 3 * 60:
        return False
    state[station_key] = (track, now)
    return True


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
    rows.append(
        [InlineKeyboardButton(text="📋 Сводка по всем", callback_data="summary")]
    )
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


def start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🟢 Подключить Spotify"),
                KeyboardButton(text="💚 Последние лайки"),
            ],
            [
                KeyboardButton(text="📻 Старт"),
                KeyboardButton(text="📋 Все станции"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Кнопка запуска закреплена внизу 👇", reply_markup=start_keyboard()
    )
    await show_station_menu(message)


@router.message(F.text == "📻 Старт")
async def start_button(message: Message) -> None:
    await message.answer("Клавиатура обновлена 👇", reply_markup=start_keyboard())
    await show_station_menu(message)


@router.message(Command("spotify"))
@router.message(F.text == "🟢 Подключить Spotify")
async def spotify_status(message: Message) -> None:
    assert spotify_config is not None and spotify_store is not None
    if not spotify_config.is_configured:
        await message.answer(
            "🟢 <b>Интеграция со Spotify подготовлена</b>\n\n"
            "Для подключения позже добавьте SPOTIFY_CLIENT_ID, "
            "SPOTIFY_CLIENT_SECRET и SPOTIFY_REDIRECT_URI в файл .env."
        )
        return

    telegram_user_id = message.from_user.id if message.from_user else message.chat.id
    authorization_url, state = create_authorization_url(
        spotify_config, telegram_user_id
    )
    spotify_store.save_state(state, telegram_user_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подключить Spotify", url=authorization_url)]
        ]
    )
    await message.answer(
        "Настройки Spotify найдены. Можно перейти к авторизации:",
        reply_markup=keyboard,
    )


@router.message(Command("liked"))
@router.message(F.text == "💚 Последние лайки")
async def recent_liked_tracks(message: Message) -> None:
    telegram_user_id = message.from_user.id if message.from_user else message.chat.id
    access_token = await get_spotify_access_token(telegram_user_id)
    if not access_token:
        await message.answer(
            "Сначала подключите Spotify командой /spotify. "
            "Кнопка начнёт показывать последние лайки после авторизации аккаунта."
        )
        return

    assert radio_service is not None
    try:
        tracks = await fetch_recent_liked_tracks(
            radio_service.session, access_token, limit=10
        )
    except SpotifyError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}. Подключите Spotify заново.")
        return

    if not tracks:
        await message.answer("В Spotify пока нет сохранённых треков.")
        return

    lines = [
        f"{index}. <b>{html.escape(track.artist)}</b> — {html.escape(track.title)}"
        for index, track in enumerate(tracks, start=1)
    ]
    await message.answer("💚 <b>Последние лайки Spotify</b>\n\n" + "\n".join(lines))


@router.message(F.text.contains("/spotify/callback"))
async def pasted_spotify_callback(message: Message) -> None:
    """Complete local OAuth when a user pastes the unreachable callback URL."""
    parsed = urlparse(message.text or "")
    if not parsed.path.endswith("/spotify/callback"):
        return
    query = parse_qs(parsed.query)
    state = query.get("state", [""])[0]
    code = query.get("code", [""])[0]
    if not state or not code:
        await message.answer("В адресе нет данных авторизации Spotify.")
        return

    assert spotify_store is not None and spotify_config is not None
    telegram_user_id = spotify_store.consume_state(state)
    sender_id = message.from_user.id if message.from_user else message.chat.id
    if telegram_user_id is None or telegram_user_id != sender_id:
        await message.answer(
            "Ссылка авторизации устарела. Нажмите «🟢 Подключить Spotify» ещё раз."
        )
        return

    assert radio_service is not None
    try:
        token = await exchange_authorization_code(
            radio_service.session, spotify_config, code
        )
    except SpotifyError as exc:
        await message.answer(
            f"⚠️ {html.escape(str(exc))}. Попробуйте подключиться ещё раз."
        )
        return
    spotify_store.save_token(
        telegram_user_id,
        StoredSpotifyToken(token.access_token, token.refresh_token, token.expires_at),
    )
    try:
        await message.delete()
    except TelegramBadRequest:
        logger.warning("Could not delete a message containing a Spotify OAuth code")
    await message.answer(
        "✅ Spotify подключён. Повторять это после перезапуска бота не потребуется.",
        reply_markup=start_keyboard(),
    )


async def get_spotify_access_token(telegram_user_id: int) -> str | None:
    assert spotify_store is not None
    token = spotify_store.get_token(telegram_user_id)
    if token is None:
        return None
    if token.expires_at > int(time.time()) + 60:
        return token.access_token

    assert spotify_config is not None and radio_service is not None
    try:
        refreshed = await refresh_access_token(
            radio_service.session, spotify_config, token.refresh_token
        )
    except SpotifyError:
        return None
    spotify_store.save_token(
        telegram_user_id,
        StoredSpotifyToken(
            refreshed.access_token, refreshed.refresh_token, refreshed.expires_at
        ),
    )
    return refreshed.access_token


async def spotify_notification_monitor(bot: Bot) -> None:
    """Notify connected users when a liked track appears on any station."""
    liked_cache: dict[int, tuple[float, list]] = {}
    airplay_state: dict[str, tuple[str, float]] = {}
    semaphore = asyncio.Semaphore(3)

    async def read_station(station: Station) -> tuple[Station, str | None]:
        async with semaphore:
            try:
                assert radio_service is not None
                return station, await radio_service.now_playing(station)
            except RadioError:
                return station, None

    while True:
        try:
            assert radio_service is not None and spotify_store is not None
            station_tracks = await asyncio.gather(
                *(read_station(station) for station in STATIONS)
            )
            checked_at = time.monotonic()
            for station, radio_track in station_tracks:
                if radio_track and should_log_airplay(
                    airplay_state, station.key, radio_track, checked_at
                ):
                    airplay_logger.info("%s — %s", station.title, radio_track)

            for telegram_user_id in spotify_store.connected_user_ids():
                access_token = await get_spotify_access_token(telegram_user_id)
                if not access_token:
                    continue
                cached = liked_cache.get(telegram_user_id)
                if cached and cached[0] > time.monotonic():
                    liked_tracks = cached[1]
                else:
                    try:
                        liked_tracks = await fetch_all_liked_tracks(
                            radio_service.session, access_token
                        )
                    except SpotifyError:
                        continue
                    liked_cache[telegram_user_id] = (
                        time.monotonic() + 10 * 60,
                        liked_tracks,
                    )

                for station, radio_track in station_tracks:
                    if not radio_track:
                        continue
                    matched = any(
                        tracks_match(radio_track, track.artist, track.title)
                        for track in liked_tracks
                    )
                    if not matched or not spotify_store.mark_notification(
                        telegram_user_id, station.key, radio_track
                    ):
                        continue
                    await bot.send_message(
                        telegram_user_id,
                        f"💚 <b>{html.escape(station.title)}</b>\n\n"
                        "Сейчас играет трек из ваших любимых:\n"
                        f"🎵 <b>{html.escape(radio_track)}</b>",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Spotify notification monitor failed")
        await asyncio.sleep(60)


async def spotify_callback(request: web.Request) -> web.Response:
    assert spotify_store is not None and spotify_config is not None
    state = request.query.get("state", "")
    telegram_user_id = spotify_store.consume_state(state)
    if telegram_user_id is None:
        return web.Response(text="Invalid or expired Spotify authorization", status=400)
    if error := request.query.get("error"):
        return web.Response(text=f"Spotify authorization failed: {error}", status=400)
    code = request.query.get("code")
    if not code or radio_service is None:
        return web.Response(
            text="Spotify did not return an authorization code", status=400
        )

    try:
        token = await exchange_authorization_code(
            radio_service.session, spotify_config, code
        )
    except SpotifyError as exc:
        return web.Response(text=str(exc), status=502)
    spotify_store.save_token(
        telegram_user_id,
        StoredSpotifyToken(token.access_token, token.refresh_token, token.expires_at),
    )
    bot: Bot = request.app["bot"]
    await bot.send_message(
        telegram_user_id,
        "✅ Spotify подключён. Теперь кнопка «💚 Последние лайки» работает.",
        reply_markup=start_keyboard(),
    )
    return web.Response(
        text="Spotify connected. You can close this window and return to Telegram."
    )


async def build_summary() -> str:
    assert radio_service is not None
    semaphore = asyncio.Semaphore(3)

    async def check(station: Station) -> str:
        async with semaphore:
            try:
                title = await radio_service.now_playing(station)
            except RadioError:
                return f"⚠️ <b>{html.escape(station.title)}</b> — недоступна"
            if not title:
                return f"➖ <b>{html.escape(station.title)}</b> — нет данных"
            return f"🎵 <b>{html.escape(station.title)}</b> — {html.escape(title)}"

    lines = await asyncio.gather(*(check(station) for station in STATIONS))
    checked_at = datetime.now().astimezone().strftime("%H:%M:%S")
    return (
        "📋 <b>Сейчас в эфире</b>\n\n"
        + "\n".join(lines)
        + f"\n\n<i>Проверено в {checked_at}</i>"
    )


def summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="summary")],
            [
                InlineKeyboardButton(
                    text="📻 Выбрать станцию", callback_data="stations:0"
                )
            ],
        ]
    )


@router.message(Command("summary"))
@router.message(F.text == "📋 Все станции")
async def summary_message(message: Message) -> None:
    status = await message.answer("⏳ Собираю данные со всех станций…")
    await status.edit_text(await build_summary(), reply_markup=summary_keyboard())


@router.callback_query(F.data == "summary")
async def summary_callback(callback: CallbackQuery) -> None:
    await callback.answer("Собираю сводку…")
    if callback.message:
        await callback.message.edit_text("⏳ Собираю данные со всех станций…")
        await callback.message.edit_text(
            await build_summary(), reply_markup=summary_keyboard()
        )


async def show_station_menu(message: Message) -> None:
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


@router.message()
async def first_message(message: Message) -> None:
    """Показывает клавиатуру после любого первого сообщения пользователя."""
    await start(message)


async def main() -> None:
    global radio_service, spotify_config, spotify_store
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и укажите токен."
        )

    spotify_config = SpotifyConfig(
        client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", ""),
    )
    spotify_store = SpotifyStore(Path("data/spotify.sqlite3"))

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    configure_airplay_logging()
    timeout = aiohttp.ClientTimeout(total=15, connect=6, sock_read=10)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": "RadioNowTelegramBot/1.0"}
    ) as session:
        radio_service = RadioService(session)
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        callback_runner: web.AppRunner | None = None
        if spotify_config.is_configured:
            redirect = urlparse(spotify_config.redirect_uri)
            if redirect.hostname not in {"127.0.0.1", "::1"} or not redirect.port:
                raise SystemExit(
                    "SPOTIFY_REDIRECT_URI must use a local IP and an explicit port"
                )
            callback_app = web.Application()
            callback_app["bot"] = bot
            callback_app.router.add_get(redirect.path, spotify_callback)
            callback_runner = web.AppRunner(callback_app)
            await callback_runner.setup()
            callback_site = web.TCPSite(
                callback_runner, redirect.hostname, redirect.port
            )
            await callback_site.start()
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть список станций"),
                BotCommand(command="summary", description="Сводка по всем станциям"),
            ]
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        notification_task = asyncio.create_task(spotify_notification_monitor(bot))
        try:
            await dispatcher.start_polling(bot)
        finally:
            notification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notification_task
            if callback_runner is not None:
                await callback_runner.cleanup()
            await bot.session.close()
            spotify_store.close()


if __name__ == "__main__":
    asyncio.run(main())
