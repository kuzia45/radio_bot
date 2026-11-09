"""Telegram-бот, показывающий текущие треки российских радиостанций."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
)
from aiohttp import web
from dotenv import load_dotenv

from src.logging_config import configure_logging
from src.radio import STATIONS, STATIONS_BY_KEY, RadioError, RadioService, Station
from src.spotify import (
    SpotifyConfig,
    SpotifyError,
    SpotifyTrack,
    create_authorization_url,
    exchange_authorization_code,
    fetch_all_liked_tracks,
    refresh_access_token,
    tracks_match,
)
from src.spotify_store import SpotifyStore, StoredSpotifyToken, StoredYandexToken
from src.yandex_integration import (
    YandexMusicError,
    authorize_yandex_music,
    fetch_yandex_liked_tracks,
)

router = Router()
logger = logging.getLogger(__name__)
airplay_logger = logging.getLogger("airplay")
radio_service: RadioService | None = None
spotify_config: SpotifyConfig | None = None
spotify_store: SpotifyStore | None = None
STATIONS_PER_PAGE = 8
MOSCOW_TZ = timezone(timedelta(hours=3), name="Europe/Moscow")
ALL_STATION_KEYS = {station.key for station in STATIONS}


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


def positive_env_int(name: str, default: int) -> int:
    """Read a positive integer setting without breaking bot startup."""
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        logger.warning("INVALID SETTING | %s | using=%s", name, default)
        return default


def station_health_state(
    status: tuple,
    now: datetime,
    failure_threshold: int = 3,
    stale_minutes: int = 30,
) -> str:
    """Classify a station as healthy, unavailable, or stale."""
    _key, _track, _success_at, change_at, failures, _error = status
    if int(failures) >= failure_threshold:
        return "unavailable"
    if change_at:
        changed = datetime.fromisoformat(str(change_at)).astimezone(MOSCOW_TZ)
        age_minutes = (now - changed).total_seconds() / 60
        if age_minutes >= stale_minutes:
            return "stale"
    return "healthy"


async def report_station_health(
    bot: Bot,
    station: Station,
    status: tuple,
    now: datetime,
    failure_threshold: int,
    stale_minutes: int,
) -> None:
    """Log health transitions and notify the configured administrator."""
    assert spotify_store is not None
    state = station_health_state(status, now, failure_threshold, stale_minutes)
    previous = spotify_store.set_station_health(station.key, state)
    if previous is None or previous == state:
        return
    if previous == "unknown" and state == "healthy":
        return

    _key, track, _success_at, change_at, failures, error = status
    admin_user_id = int(os.getenv("ADMIN_USER_ID", "0") or 0)
    if state == "healthy":
        logger.info(
            "SOURCE RECOVERED | station=%s | previous=%s", station.key, previous
        )
        message = (
            f"🟢 <b>{html.escape(station.title)} снова работает</b>\n\n"
            f"Сейчас играет: <b>{html.escape(str(track))}</b>"
        )
    elif state == "unavailable":
        logger.error(
            "SOURCE UNAVAILABLE | station=%s | failures=%s | error=%s",
            station.key,
            failures,
            error,
        )
        message = (
            f"🔴 <b>Источник {html.escape(station.title)} недоступен</b>\n\n"
            f"Ошибок подряд: {int(failures)}\n"
            f"Причина: {html.escape(str(error or 'нет данных'))}"
        )
    else:
        changed = datetime.fromisoformat(str(change_at)).astimezone(MOSCOW_TZ)
        minutes = int((now - changed).total_seconds() // 60)
        logger.warning(
            "SOURCE STALE | station=%s | minutes=%s | track=%s",
            station.key,
            minutes,
            track,
        )
        message = (
            f"🟡 <b>Возможно, завис источник {html.escape(station.title)}</b>\n\n"
            f"Трек не менялся {minutes} мин.:\n"
            f"<b>{html.escape(str(track))}</b>"
        )

    if not admin_user_id:
        return
    try:
        await bot.send_message(admin_user_id, message)
    except Exception:
        logger.exception(
            "ADMIN ALERT FAILED | station=%s | state=%s", station.key, state
        )


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


def music_links(track: str) -> list[InlineKeyboardButton]:
    query = quote_plus(track)
    return [
        InlineKeyboardButton(
            text="Spotify", url=f"https://open.spotify.com/search/{query}"
        ),
        InlineKeyboardButton(
            text="Яндекс Музыка", url=f"https://music.yandex.ru/search?text={query}"
        ),
        InlineKeyboardButton(
            text="YouTube", url=f"https://www.youtube.com/results?search_query={query}"
        ),
    ]


def result_keyboard(
    station: Station, telegram_user_id: int, track: str | None = None
) -> InlineKeyboardMarkup:
    assert spotify_store is not None
    is_favorite = station.key in spotify_store.favorite_station_keys(telegram_user_id)
    rows = []
    if track:
        rows.append(music_links(track))
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data=f"station:{station.key}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 10 последних треков",
                    callback_data=f"history:{station.key}",
                ),
                InlineKeyboardButton(
                    text="⭐ Убрать" if is_favorite else "☆ В избранное",
                    callback_data=f"favorite:{station.key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📻 Другие станции", callback_data="stations:0"
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🟢 Подключить Spotify"),
                KeyboardButton(text="🟡 Подключить Яндекс Музыку"),
            ],
            [
                KeyboardButton(text="💚 Лайки Spotify"),
                KeyboardButton(text="💛 Лайки Яндекс"),
            ],
            [
                KeyboardButton(text="📻 Старт"),
                KeyboardButton(text="📋 Все станции"),
            ],
            [
                KeyboardButton(text="⭐ Мои станции"),
                KeyboardButton(text="⚙️ Уведомления"),
            ],
            [KeyboardButton(text="📊 Итоги дня")],
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
@router.message(F.text == "💚 Лайки Spotify")
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

    tracks, sync_error = await sync_spotify_library(telegram_user_id, access_token)

    if not tracks:
        if sync_error:
            await message.answer(
                f"⚠️ {html.escape(sync_error)}. Следующая синхронизация — завтра."
            )
        else:
            await message.answer("В Spotify пока нет сохранённых треков.")
        return

    lines = [
        f"{index}. <b>{html.escape(track.artist)}</b> — {html.escape(track.title)}"
        for index, track in enumerate(tracks[:10], start=1)
    ]
    suffix = f"\n\n⚠️ Последний кеш: {html.escape(sync_error)}" if sync_error else ""
    await message.answer(
        "💚 <b>Последние лайки Spotify</b>\n\n" + "\n".join(lines) + suffix
    )


@router.message(Command("yandex"))
@router.message(F.text == "🟡 Подключить Яндекс Музыку")
async def connect_yandex_music(message: Message) -> None:
    assert spotify_store is not None
    telegram_user_id = message.from_user.id if message.from_user else message.chat.id

    async def show_code(code) -> None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Скопировать код",
                        copy_text=CopyTextButton(text=code.user_code),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Открыть Яндекс", url=code.verification_url
                    )
                ],
            ]
        )
        await message.answer(
            "🟡 <b>Подключение Яндекс Музыки</b>\n\n"
            f"Откройте страницу Яндекса и введите код:\n"
            f"<code>{html.escape(code.user_code)}</code>\n\n"
            f"Код действует {code.expires_in // 60} мин. Пароль вводится только на стороне Яндекса.",
            reply_markup=keyboard,
        )

    try:
        access_token, refresh_token, expires_in = await authorize_yandex_music(
            show_code
        )
    except YandexMusicError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}. Попробуйте ещё раз.")
        return
    expires_at = int(time.time()) + expires_in if expires_in else None
    spotify_store.save_yandex_token(
        telegram_user_id,
        StoredYandexToken(access_token, refresh_token, expires_at),
    )
    logger.info("YANDEX CONNECTED | user=%s", telegram_user_id)
    await message.answer(
        "✅ Яндекс Музыка подключена. Лайки будут участвовать в уведомлениях.",
        reply_markup=start_keyboard(),
    )


@router.message(F.text == "💛 Лайки Яндекс")
async def recent_yandex_liked_tracks(message: Message) -> None:
    assert spotify_store is not None
    telegram_user_id = message.from_user.id if message.from_user else message.chat.id
    token = spotify_store.get_yandex_token(telegram_user_id)
    if token is None:
        await message.answer("Сначала нажмите «🟡 Подключить Яндекс Музыку».")
        return
    try:
        tracks = await fetch_yandex_liked_tracks(token.access_token, limit=10)
    except YandexMusicError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}. Подключите Яндекс заново.")
        return
    if not tracks:
        await message.answer("В Яндекс Музыке пока нет понравившихся треков.")
        return
    lines = [
        f"{index}. <b>{html.escape(track.artist)}</b> — {html.escape(track.title)}"
        for index, track in enumerate(tracks, start=1)
    ]
    await message.answer(
        "💛 <b>Последние лайки Яндекс Музыки</b>\n\n" + "\n".join(lines)
    )


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
    spotify_store.reset_spotify_sync_attempt(telegram_user_id)
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


async def sync_spotify_library(
    telegram_user_id: int, access_token: str
) -> tuple[list[SpotifyTrack], str | None]:
    """Refresh a user's Spotify library at most once per Moscow calendar day."""
    assert spotify_store is not None and radio_service is not None
    date = datetime.now(MOSCOW_TZ).date().isoformat()
    sync_error: str | None = None
    if spotify_store.spotify_sync_due(telegram_user_id, date):
        spotify_store.mark_spotify_sync_attempt(telegram_user_id, date)
        try:
            tracks = await fetch_all_liked_tracks(radio_service.session, access_token)
        except SpotifyError as exc:
            sync_error = str(exc)
            spotify_store.save_spotify_sync_error(telegram_user_id, sync_error)
            logger.warning(
                "SPOTIFY SYNC FAILED | user=%s | error=%s",
                telegram_user_id,
                sync_error,
            )
        else:
            spotify_store.save_spotify_liked_tracks(
                telegram_user_id,
                [(track.artist, track.title) for track in tracks],
            )
            logger.info(
                "SPOTIFY SYNC OK | user=%s | tracks=%s",
                telegram_user_id,
                len(tracks),
            )

    cached = spotify_store.cached_spotify_liked_tracks(telegram_user_id)
    if sync_error is None:
        status = spotify_store.spotify_sync_status(telegram_user_id)
        if status and status[2]:
            sync_error = str(status[2])
    return [SpotifyTrack(artist, title) for artist, title in cached], sync_error


async def spotify_notification_monitor(bot: Bot) -> None:
    """Notify connected users when a liked track appears on any station."""
    liked_cache: dict[tuple[str, int], tuple[float, list]] = {}
    airplay_state: dict[str, tuple[str, float]] = {}
    semaphore = asyncio.Semaphore(3)
    failure_threshold = positive_env_int("SOURCE_FAILURE_THRESHOLD", 3)
    stale_minutes = positive_env_int("SOURCE_STALE_MINUTES", 30)

    async def read_station(
        station: Station,
    ) -> tuple[Station, str | None, str | None]:
        async with semaphore:
            try:
                assert radio_service is not None
                track = await radio_service.now_playing(station)
                return station, track, None if track else "Нет данных"
            except RadioError as exc:
                return station, None, str(exc)

    while True:
        cycle_started = time.monotonic()
        try:
            assert radio_service is not None and spotify_store is not None
            station_tracks = await asyncio.gather(
                *(read_station(station) for station in STATIONS)
            )
            checked_at = time.monotonic()
            for station, radio_track, error in station_tracks:
                spotify_store.update_station_status(station.key, radio_track, error)
                if radio_track and should_log_airplay(
                    airplay_state, station.key, radio_track, checked_at
                ):
                    airplay_logger.info("%s — %s", station.title, radio_track)
                    spotify_store.record_airplay(station.key, radio_track)

            statuses = {row[0]: row for row in spotify_store.station_statuses()}
            checked_at_datetime = datetime.now(MOSCOW_TZ)
            for station in STATIONS:
                status = statuses.get(station.key)
                if status:
                    await report_station_health(
                        bot,
                        station,
                        status,
                        checked_at_datetime,
                        failure_threshold,
                        stale_minutes,
                    )

            user_ids = spotify_store.music_user_ids()
            matches_found = 0
            messages_sent = 0
            for telegram_user_id in user_ids:
                liked_tracks = []
                access_token = await get_spotify_access_token(telegram_user_id)
                if access_token:
                    spotify_tracks, _sync_error = await sync_spotify_library(
                        telegram_user_id, access_token
                    )
                    liked_tracks.extend(spotify_tracks)

                yandex_token = spotify_store.get_yandex_token(telegram_user_id)
                if yandex_token:
                    cache_key = ("yandex", telegram_user_id)
                    cached = liked_cache.get(cache_key)
                    if cached and cached[0] > time.monotonic():
                        liked_tracks.extend(cached[1])
                    else:
                        try:
                            yandex_tracks = await fetch_yandex_liked_tracks(
                                yandex_token.access_token
                            )
                        except YandexMusicError:
                            yandex_tracks = []
                        liked_cache[cache_key] = (
                            time.monotonic() + 10 * 60,
                            yandex_tracks,
                        )
                        liked_tracks.extend(yandex_tracks)

                if not liked_tracks:
                    continue

                selected_stations = spotify_store.notification_station_keys(
                    telegram_user_id, ALL_STATION_KEYS
                )
                for station, radio_track, _ in station_tracks:
                    if not radio_track or station.key not in selected_stations:
                        continue
                    matched = any(
                        tracks_match(radio_track, track.artist, track.title)
                        for track in liked_tracks
                    )
                    if not matched or not spotify_store.mark_notification(
                        telegram_user_id, station.key, radio_track
                    ):
                        continue
                    quiet = is_quiet_time(
                        datetime.now(MOSCOW_TZ).hour,
                        spotify_store.quiet_hours(telegram_user_id),
                    )
                    spotify_store.record_match(
                        telegram_user_id,
                        station.key,
                        radio_track,
                        delivered=not quiet,
                    )
                    matches_found += 1
                    if not quiet:
                        await bot.send_message(
                            telegram_user_id,
                            f"💚 <b>{html.escape(station.title)}</b>\n\n"
                            "Сейчас играет трек из ваших любимых:\n"
                            f"🎵 <b>{html.escape(radio_track)}</b>",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[music_links(radio_track)]
                            ),
                        )
                        messages_sent += 1

                now = datetime.now(MOSCOW_TZ)
                date = now.date().isoformat()
                if spotify_store.digest_due(telegram_user_id, date, now.hour):
                    await bot.send_message(
                        telegram_user_id, build_daily_digest(telegram_user_id, date)
                    )
                    spotify_store.mark_digest_sent(telegram_user_id, date)
            available_count = sum(1 for _, track, _ in station_tracks if track)
            unavailable = [
                station.key for station, track, _ in station_tracks if not track
            ]
            logger.info(
                "RADIO CHECK | stations=%s/%s | users=%s | "
                "matches=%s | sent=%s | %.1fs",
                available_count,
                len(STATIONS),
                len(user_ids),
                matches_found,
                messages_sent,
                time.monotonic() - cycle_started,
            )
            if unavailable:
                logger.warning("RADIO NO DATA | %s", ", ".join(unavailable))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MONITOR ERROR")
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
    spotify_store.reset_spotify_sync_attempt(telegram_user_id)
    logger.info("SPOTIFY CONNECTED | user=%s", telegram_user_id)
    bot: Bot = request.app["bot"]
    await bot.send_message(
        telegram_user_id,
        "✅ Spotify подключён. Теперь кнопка «💚 Последние лайки» работает.",
        reply_markup=start_keyboard(),
    )
    return web.Response(
        text="Spotify connected. You can close this window and return to Telegram."
    )


async def health_check(_: web.Request) -> web.Response:
    """Confirm to the hosting platform that the bot process is alive."""
    return web.Response(text="OK")


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
    checked_at = datetime.now(MOSCOW_TZ).strftime("%H:%M:%S")
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


def is_quiet_time(hour: int, quiet_hours: tuple[int, int] | None) -> bool:
    if quiet_hours is None:
        return False
    start, end = quiet_hours
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def build_daily_digest(telegram_user_id: int, date: str | None = None) -> str:
    assert spotify_store is not None
    date = date or datetime.now(MOSCOW_TZ).date().isoformat()
    matches = spotify_store.daily_matches(telegram_user_id, date)
    if not matches:
        return "📊 <b>Итоги дня</b>\n\nСегодня совпадений с любимыми треками не было."
    by_station: dict[str, int] = {}
    for station_key, _, _, _ in matches:
        by_station[station_key] = by_station.get(station_key, 0) + 1
    station_lines = [
        f"• {html.escape(STATIONS_BY_KEY[key].title)} — {count}"
        for key, count in sorted(by_station.items(), key=lambda item: -item[1])
        if key in STATIONS_BY_KEY
    ]
    recent = [
        f"• {html.escape(STATIONS_BY_KEY[key].title)}: {html.escape(track)}"
        for key, track, _, _ in matches[-5:]
        if key in STATIONS_BY_KEY
    ]
    return (
        f"📊 <b>Итоги дня</b>\n\nСовпадений: <b>{len(matches)}</b>\n\n"
        + "<b>По станциям:</b>\n"
        + "\n".join(station_lines)
        + "\n\n<b>Последние:</b>\n"
        + "\n".join(recent)
    )


@router.message(Command("summary"))
@router.message(F.text == "📋 Все станции")
async def summary_message(message: Message) -> None:
    status = await message.answer("⏳ Собираю данные со всех станций…")
    await status.edit_text(await build_summary(), reply_markup=summary_keyboard())


@router.message(F.text == "📊 Итоги дня")
async def daily_digest_message(message: Message) -> None:
    telegram_user_id = message.from_user.id if message.from_user else message.chat.id
    await message.answer(build_daily_digest(telegram_user_id))


def notification_settings_keyboard(telegram_user_id: int) -> InlineKeyboardMarkup:
    assert spotify_store is not None
    selected = spotify_store.notification_station_keys(
        telegram_user_id, ALL_STATION_KEYS
    )
    rows = []
    for index in range(0, len(STATIONS), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if station.key in selected else "⬜ ") + station.title,
                    callback_data=f"notify:{station.key}",
                )
                for station in STATIONS[index : index + 2]
            ]
        )
    quiet = spotify_store.quiet_hours(telegram_user_id)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=("✅ " if quiet is None else "") + "Без тихих часов",
                    callback_data="quiet:off",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ " if quiet == (23, 8) else "") + "23:00–08:00",
                    callback_data="quiet:23-8",
                ),
                InlineKeyboardButton(
                    text=("✅ " if quiet == (22, 9) else "") + "22:00–09:00",
                    callback_data="quiet:22-9",
                ),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Уведомления")
async def notification_settings(message: Message) -> None:
    telegram_user_id = message.from_user.id if message.from_user else message.chat.id
    await message.answer(
        "⚙️ <b>Уведомления</b>\n\n"
        "Выберите станции и при необходимости включите тихие часы. "
        "Ежедневная сводка приходит в 20:00 по Москве.",
        reply_markup=notification_settings_keyboard(telegram_user_id),
    )


@router.callback_query(F.data.startswith("notify:"))
async def toggle_notification_station(callback: CallbackQuery) -> None:
    assert spotify_store is not None
    station_key = (callback.data or "").partition(":")[2]
    if station_key not in STATIONS_BY_KEY:
        await callback.answer("Станция не найдена", show_alert=True)
        return
    enabled = spotify_store.toggle_notification_station(
        callback.from_user.id, station_key, ALL_STATION_KEYS
    )
    await callback.answer("Включено" if enabled else "Выключено")
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=notification_settings_keyboard(callback.from_user.id)
        )


@router.callback_query(F.data.startswith("quiet:"))
async def set_quiet_hours(callback: CallbackQuery) -> None:
    assert spotify_store is not None
    value = (callback.data or "quiet:off").partition(":")[2]
    hours = None if value == "off" else tuple(map(int, value.split("-")))
    spotify_store.set_quiet_hours(callback.from_user.id, hours)
    await callback.answer("Тихие часы обновлены")
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=notification_settings_keyboard(callback.from_user.id)
        )


@router.message(F.text == "⭐ Мои станции")
async def favorite_stations(message: Message) -> None:
    assert spotify_store is not None
    telegram_user_id = message.from_user.id if message.from_user else message.chat.id
    favorite_keys = spotify_store.favorite_station_keys(telegram_user_id)
    stations = [station for station in STATIONS if station.key in favorite_keys]
    if not stations:
        await message.answer(
            "⭐ Избранных станций пока нет. Откройте станцию и нажмите «В избранное»."
        )
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=station.title, callback_data=f"station:{station.key}"
                )
            ]
            for station in stations
        ]
    )
    await message.answer("⭐ <b>Мои станции</b>", reply_markup=keyboard)


@router.callback_query(F.data.startswith("favorite:"))
async def toggle_favorite_station(callback: CallbackQuery) -> None:
    assert spotify_store is not None
    station_key = (callback.data or "").partition(":")[2]
    station = STATIONS_BY_KEY.get(station_key)
    if station is None:
        return
    added = spotify_store.toggle_favorite(callback.from_user.id, station_key)
    await callback.answer("Добавлено в избранное" if added else "Удалено из избранного")
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=result_keyboard(station, callback.from_user.id)
        )


@router.callback_query(F.data.startswith("history:"))
async def station_history(callback: CallbackQuery) -> None:
    assert spotify_store is not None
    station_key = (callback.data or "").partition(":")[2]
    station = STATIONS_BY_KEY.get(station_key)
    if station is None or callback.message is None:
        return
    history = spotify_store.recent_airplay(station_key, limit=10)
    if not history:
        text = "История ещё не накопилась. Вернитесь через несколько минут."
    else:
        lines = []
        for index, (track, played_at) in enumerate(history, start=1):
            played_time = datetime.fromisoformat(played_at).astimezone(MOSCOW_TZ)
            lines.append(f"{index}. {played_time:%H:%M} — {html.escape(track)}")
        text = "\n".join(lines)
    await callback.answer()
    await callback.message.edit_text(
        f"🎵 <b>{html.escape(station.title)}: 10 последних треков</b>\n\n{text}",
        reply_markup=result_keyboard(station, callback.from_user.id),
    )


@router.message(Command("admin_sources"))
async def admin_source_status(message: Message) -> None:
    admin_user_id = int(os.getenv("ADMIN_USER_ID", "0") or 0)
    sender_id = message.from_user.id if message.from_user else 0
    if not admin_user_id or sender_id != admin_user_id:
        return
    assert spotify_store is not None
    statuses = {row[0]: row for row in spotify_store.station_statuses()}
    now = datetime.now(MOSCOW_TZ)
    lines = ["🛠 <b>Состояние источников</b>"]
    for station in STATIONS:
        row = statuses.get(station.key)
        if not row:
            lines.append(f"⚪ {station.title} — ещё не проверялась")
            continue
        _, track, _success_at, change_at, failures, error = row
        stale_minutes = None
        if change_at:
            changed = datetime.fromisoformat(change_at).astimezone(MOSCOW_TZ)
            stale_minutes = int((now - changed).total_seconds() // 60)
        icon = (
            "🔴" if failures else "🟡" if stale_minutes and stale_minutes > 30 else "🟢"
        )
        detail = (
            f"ошибок подряд: {failures}"
            if failures
            else f"без смены: {stale_minutes or 0} мин"
        )
        lines.append(f"{icon} {station.title} — {detail}")
        if error and failures:
            lines.append(f"   {html.escape(str(error))}")
        elif track:
            lines.append(f"   {html.escape(str(track))}")
    await message.answer("\n".join(lines))


@router.message(Command("id"))
async def show_user_id(message: Message) -> None:
    """Show the Telegram ID needed for administrator configuration."""
    sender_id = message.from_user.id if message.from_user else message.chat.id
    await message.answer(f"Ваш Telegram ID: <code>{sender_id}</code>")


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

    assert radio_service is not None and spotify_store is not None
    title: str | None = None
    try:
        title = await radio_service.now_playing(station)
        if title:
            spotify_store.record_airplay(station.key, title)
            spotify_store.update_station_status(station.key, title)
            body = f"🎙 <b>{html.escape(station.title)}</b>\n\nСейчас играет:\n🎵 <b>{html.escape(title)}</b>"
        else:
            spotify_store.update_station_status(station.key, None, "Нет данных")
            body = (
                f"🎙 <b>{html.escape(station.title)}</b>\n\n"
                "Станция сейчас не передаёт название трека в эфирном потоке."
            )
    except RadioError as exc:
        spotify_store.update_station_status(station.key, None, str(exc))
        body = f"🎙 <b>{html.escape(station.title)}</b>\n\n⚠️ {html.escape(str(exc))}. Попробуйте ещё раз."

    checked_at = datetime.now(MOSCOW_TZ).strftime("%H:%M:%S")
    await callback.message.edit_text(
        f"{body}\n\n<i>Проверено в {checked_at}</i>",
        reply_markup=result_keyboard(station, callback.from_user.id, title),
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
    database_path = Path(os.getenv("DATABASE_PATH", "data/spotify.sqlite3"))
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    configure_logging(log_dir, airplay_logger)
    logger.info(
        "START | database=%s | spotify=%s | port=%s",
        database_path,
        "configured" if spotify_config.is_configured else "disabled",
        os.getenv("PORT", "local"),
    )
    spotify_store = SpotifyStore(database_path)
    timeout = aiohttp.ClientTimeout(total=15, connect=6, sock_read=10)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": "RadioNowTelegramBot/1.0"}
    ) as session:
        radio_service = RadioService(session)
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        callback_runner: web.AppRunner | None = None
        railway_port = os.getenv("PORT")
        if spotify_config.is_configured or railway_port:
            redirect = urlparse(spotify_config.redirect_uri)
            is_local = redirect.hostname in {"127.0.0.1", "::1"}
            server_host = os.getenv(
                "HOST", redirect.hostname if is_local else "0.0.0.0"
            )
            server_port = int(railway_port or redirect.port or 8080)
            callback_app = web.Application()
            callback_app["bot"] = bot
            callback_app.router.add_get("/health", health_check)
            if spotify_config.is_configured:
                callback_path = redirect.path or "/spotify/callback"
                callback_app.router.add_get(callback_path, spotify_callback)
            callback_runner = web.AppRunner(callback_app)
            await callback_runner.setup()
            callback_site = web.TCPSite(callback_runner, server_host, server_port)
            await callback_site.start()
            logger.info(
                "HTTP READY | host=%s | port=%s | callback=%s",
                server_host,
                server_port,
                redirect.path if spotify_config.is_configured else "disabled",
            )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть список станций"),
                BotCommand(command="summary", description="Сводка по всем станциям"),
                BotCommand(command="settings", description="Настроить уведомления"),
                BotCommand(command="id", description="Показать мой Telegram ID"),
            ]
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        notification_task = asyncio.create_task(spotify_notification_monitor(bot))
        bot_info = await bot.get_me()
        logger.info("BOT READY | @%s | id=%s", bot_info.username, bot_info.id)
        try:
            await dispatcher.start_polling(bot)
        finally:
            logger.info("STOP | RadioBot is shutting down")
            notification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notification_task
            if callback_runner is not None:
                await callback_runner.cleanup()
            await bot.session.close()
            spotify_store.close()


if __name__ == "__main__":
    asyncio.run(main())
