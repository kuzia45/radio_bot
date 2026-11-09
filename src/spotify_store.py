"""Persistent storage for Spotify OAuth states and user tokens."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredSpotifyToken:
    access_token: str
    refresh_token: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class StoredYandexToken:
    access_token: str
    refresh_token: str | None
    expires_at: int | None


class SpotifyStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS spotify_tokens (
                telegram_user_id INTEGER PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS spotify_oauth_states (
                state TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS spotify_notifications (
                telegram_user_id INTEGER NOT NULL,
                station_key TEXT NOT NULL,
                radio_track TEXT NOT NULL,
                notified_at TEXT NOT NULL,
                PRIMARY KEY (telegram_user_id, station_key)
            )
            """
        )
        notification_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(spotify_notifications)"
            ).fetchall()
        }
        if "notified_at" not in notification_columns:
            self.connection.execute(
                "ALTER TABLE spotify_notifications ADD COLUMN notified_at TEXT"
            )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS airplay_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_key TEXT NOT NULL,
                track TEXT NOT NULL,
                played_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spotify_liked_tracks (
                telegram_user_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                PRIMARY KEY (telegram_user_id, position)
            );
            CREATE TABLE IF NOT EXISTS spotify_library_sync (
                telegram_user_id INTEGER PRIMARY KEY,
                last_attempt_date TEXT,
                last_success_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_airplay_station_time
                ON airplay_history (station_key, played_at DESC);

            CREATE TABLE IF NOT EXISTS user_settings (
                telegram_user_id INTEGER PRIMARY KEY,
                quiet_start INTEGER,
                quiet_end INTEGER,
                daily_digest_hour INTEGER NOT NULL DEFAULT 20,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                station_filter_enabled INTEGER NOT NULL DEFAULT 0,
                last_digest_date TEXT
            );
            CREATE TABLE IF NOT EXISTS notification_stations (
                telegram_user_id INTEGER NOT NULL,
                station_key TEXT NOT NULL,
                PRIMARY KEY (telegram_user_id, station_key)
            );
            CREATE TABLE IF NOT EXISTS favorite_stations (
                telegram_user_id INTEGER NOT NULL,
                station_key TEXT NOT NULL,
                PRIMARY KEY (telegram_user_id, station_key)
            );
            CREATE TABLE IF NOT EXISTS notification_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                station_key TEXT NOT NULL,
                radio_track TEXT NOT NULL,
                matched_at TEXT NOT NULL,
                delivered INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_history_user_time
                ON notification_history (telegram_user_id, matched_at DESC);

            CREATE TABLE IF NOT EXISTS station_status (
                station_key TEXT PRIMARY KEY,
                current_track TEXT,
                last_success_at TEXT,
                last_change_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS station_health (
                station_key TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                changed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS yandex_tokens (
                telegram_user_id INTEGER PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at INTEGER
            );
            """
        )
        self.connection.commit()

    def save_state(self, state: str, telegram_user_id: int) -> None:
        expires_at = int(time.time()) + 10 * 60
        self.connection.execute(
            "INSERT OR REPLACE INTO spotify_oauth_states VALUES (?, ?, ?)",
            (state, telegram_user_id, expires_at),
        )
        self.connection.commit()

    def consume_state(self, state: str) -> int | None:
        row = self.connection.execute(
            "SELECT telegram_user_id, expires_at FROM spotify_oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        self.connection.execute(
            "DELETE FROM spotify_oauth_states WHERE state = ?", (state,)
        )
        self.connection.commit()
        if row is None or row[1] < int(time.time()):
            return None
        return int(row[0])

    def save_token(self, telegram_user_id: int, token: StoredSpotifyToken) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO spotify_tokens VALUES (?, ?, ?, ?)",
            (
                telegram_user_id,
                token.access_token,
                token.refresh_token,
                token.expires_at,
            ),
        )
        self.connection.commit()

    def get_token(self, telegram_user_id: int) -> StoredSpotifyToken | None:
        row = self.connection.execute(
            "SELECT access_token, refresh_token, expires_at FROM spotify_tokens "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return StoredSpotifyToken(*row) if row else None

    def spotify_sync_due(self, telegram_user_id: int, date: str) -> bool:
        row = self.connection.execute(
            "SELECT last_attempt_date FROM spotify_library_sync "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return not row or row[0] != date

    def mark_spotify_sync_attempt(self, telegram_user_id: int, date: str) -> None:
        self.connection.execute(
            "INSERT INTO spotify_library_sync "
            "(telegram_user_id, last_attempt_date) VALUES (?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "last_attempt_date = excluded.last_attempt_date",
            (telegram_user_id, date),
        )
        self.connection.commit()

    def reset_spotify_sync_attempt(self, telegram_user_id: int) -> None:
        """Allow a fresh library sync after the user reconnects Spotify."""
        self.connection.execute(
            "UPDATE spotify_library_sync SET last_attempt_date = NULL "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        self.connection.commit()

    def save_spotify_liked_tracks(
        self, telegram_user_id: int, tracks: list[tuple[str, str]]
    ) -> None:
        """Replace a user's cached Spotify library after a successful sync."""
        with self.connection:
            self.connection.execute(
                "DELETE FROM spotify_liked_tracks WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            self.connection.executemany(
                "INSERT INTO spotify_liked_tracks VALUES (?, ?, ?, ?)",
                (
                    (telegram_user_id, position, artist, title)
                    for position, (artist, title) in enumerate(tracks)
                ),
            )
            self.connection.execute(
                "UPDATE spotify_library_sync SET last_success_at = ?, "
                "last_error = NULL WHERE telegram_user_id = ?",
                (self._now(), telegram_user_id),
            )

    def save_spotify_sync_error(self, telegram_user_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE spotify_library_sync SET last_error = ? WHERE telegram_user_id = ?",
            (error, telegram_user_id),
        )
        self.connection.commit()

    def cached_spotify_liked_tracks(
        self, telegram_user_id: int, limit: int | None = None
    ) -> list[tuple[str, str]]:
        query = (
            "SELECT artist, title FROM spotify_liked_tracks "
            "WHERE telegram_user_id = ? ORDER BY position"
        )
        params: tuple[int, ...] = (telegram_user_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (telegram_user_id, limit)
        return [
            (str(artist), str(title))
            for artist, title in self.connection.execute(query, params).fetchall()
        ]

    def spotify_sync_status(self, telegram_user_id: int) -> tuple | None:
        return self.connection.execute(
            "SELECT last_attempt_date, last_success_at, last_error "
            "FROM spotify_library_sync WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()

    def connected_user_ids(self) -> list[int]:
        rows = self.connection.execute(
            "SELECT telegram_user_id FROM spotify_tokens"
        ).fetchall()
        return [int(row[0]) for row in rows]

    def music_user_ids(self) -> list[int]:
        rows = self.connection.execute(
            "SELECT telegram_user_id FROM spotify_tokens "
            "UNION SELECT telegram_user_id FROM yandex_tokens"
        ).fetchall()
        return [int(row[0]) for row in rows]

    def save_yandex_token(
        self, telegram_user_id: int, token: StoredYandexToken
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO yandex_tokens VALUES (?, ?, ?, ?)",
            (
                telegram_user_id,
                token.access_token,
                token.refresh_token,
                token.expires_at,
            ),
        )
        self.connection.commit()

    def get_yandex_token(self, telegram_user_id: int) -> StoredYandexToken | None:
        row = self.connection.execute(
            "SELECT access_token, refresh_token, expires_at FROM yandex_tokens "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return StoredYandexToken(*row) if row else None

    def mark_notification(
        self, telegram_user_id: int, station_key: str, radio_track: str
    ) -> bool:
        """Return True once for a new track on a station and persist the marker."""
        row = self.connection.execute(
            "SELECT radio_track FROM spotify_notifications "
            "WHERE telegram_user_id = ? AND station_key = ?",
            (telegram_user_id, station_key),
        ).fetchone()
        if row and row[0] == radio_track:
            return False
        self.connection.execute(
            "INSERT OR REPLACE INTO spotify_notifications "
            "(telegram_user_id, station_key, radio_track, notified_at) "
            "VALUES (?, ?, ?, ?)",
            (
                telegram_user_id,
                station_key,
                radio_track,
                self._now(),
            ),
        )
        self.connection.commit()
        return True

    def record_airplay(self, station_key: str, track: str) -> bool:
        """Store a track only when it differs from the latest station entry."""
        latest = self.connection.execute(
            "SELECT track FROM airplay_history WHERE station_key = ? "
            "ORDER BY id DESC LIMIT 1",
            (station_key,),
        ).fetchone()
        if latest and latest[0] == track:
            return False
        self.connection.execute(
            "INSERT INTO airplay_history (station_key, track, played_at) "
            "VALUES (?, ?, ?)",
            (station_key, track, self._now()),
        )
        self.connection.commit()
        return True

    def recent_airplay(
        self, station_key: str, limit: int = 10
    ) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT track, played_at FROM airplay_history WHERE station_key = ? "
            "ORDER BY id DESC LIMIT ?",
            (station_key, limit),
        ).fetchall()
        return [(str(track), str(played_at)) for track, played_at in rows]

    def favorite_station_keys(self, telegram_user_id: int) -> set[str]:
        rows = self.connection.execute(
            "SELECT station_key FROM favorite_stations WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def toggle_favorite(self, telegram_user_id: int, station_key: str) -> bool:
        favorite = station_key in self.favorite_station_keys(telegram_user_id)
        if favorite:
            self.connection.execute(
                "DELETE FROM favorite_stations WHERE telegram_user_id = ? "
                "AND station_key = ?",
                (telegram_user_id, station_key),
            )
        else:
            self.connection.execute(
                "INSERT OR IGNORE INTO favorite_stations VALUES (?, ?)",
                (telegram_user_id, station_key),
            )
        self.connection.commit()
        return not favorite

    def notification_station_keys(
        self, telegram_user_id: int, all_station_keys: set[str]
    ) -> set[str]:
        row = self.connection.execute(
            "SELECT station_filter_enabled FROM user_settings "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row is None or not row[0]:
            return set(all_station_keys)
        rows = self.connection.execute(
            "SELECT station_key FROM notification_stations WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchall()
        return {str(item[0]) for item in rows}

    def toggle_notification_station(
        self, telegram_user_id: int, station_key: str, all_station_keys: set[str]
    ) -> bool:
        selected = self.notification_station_keys(telegram_user_id, all_station_keys)
        self._ensure_settings(telegram_user_id)
        self.connection.execute(
            "UPDATE user_settings SET station_filter_enabled = 1 "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        self.connection.execute(
            "DELETE FROM notification_stations WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        if station_key in selected:
            selected.remove(station_key)
        else:
            selected.add(station_key)
        self.connection.executemany(
            "INSERT INTO notification_stations VALUES (?, ?)",
            ((telegram_user_id, key) for key in selected),
        )
        self.connection.commit()
        return station_key in selected

    def quiet_hours(self, telegram_user_id: int) -> tuple[int, int] | None:
        row = self.connection.execute(
            "SELECT quiet_start, quiet_end FROM user_settings "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    def set_quiet_hours(
        self, telegram_user_id: int, hours: tuple[int, int] | None
    ) -> None:
        self._ensure_settings(telegram_user_id)
        values = hours or (None, None)
        self.connection.execute(
            "UPDATE user_settings SET quiet_start = ?, quiet_end = ? "
            "WHERE telegram_user_id = ?",
            (*values, telegram_user_id),
        )
        self.connection.commit()

    def record_match(
        self,
        telegram_user_id: int,
        station_key: str,
        radio_track: str,
        delivered: bool,
    ) -> None:
        self.connection.execute(
            "INSERT INTO notification_history "
            "(telegram_user_id, station_key, radio_track, matched_at, delivered) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_user_id, station_key, radio_track, self._now(), int(delivered)),
        )
        self.connection.commit()

    def daily_matches(
        self, telegram_user_id: int, date_prefix: str
    ) -> list[tuple[str, str, str, bool]]:
        rows = self.connection.execute(
            "SELECT station_key, radio_track, matched_at, delivered "
            "FROM notification_history WHERE telegram_user_id = ? "
            "AND matched_at LIKE ? ORDER BY id",
            (telegram_user_id, f"{date_prefix}%"),
        ).fetchall()
        return [
            (str(key), str(track), str(matched_at), bool(delivered))
            for key, track, matched_at, delivered in rows
        ]

    def digest_due(self, telegram_user_id: int, date: str, hour: int) -> bool:
        self._ensure_settings(telegram_user_id)
        row = self.connection.execute(
            "SELECT daily_digest_hour, last_digest_date FROM user_settings "
            "WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return bool(row and int(row[0]) == hour and row[1] != date)

    def mark_digest_sent(self, telegram_user_id: int, date: str) -> None:
        self.connection.execute(
            "UPDATE user_settings SET last_digest_date = ? WHERE telegram_user_id = ?",
            (date, telegram_user_id),
        )
        self.connection.commit()

    def update_station_status(
        self, station_key: str, track: str | None, error: str | None = None
    ) -> None:
        now = self._now()
        previous = self.connection.execute(
            "SELECT current_track, last_change_at, consecutive_failures "
            "FROM station_status WHERE station_key = ?",
            (station_key,),
        ).fetchone()
        if track:
            changed_at = now if not previous or previous[0] != track else previous[1]
            self.connection.execute(
                "INSERT INTO station_status VALUES (?, ?, ?, ?, 0, NULL) "
                "ON CONFLICT(station_key) DO UPDATE SET current_track = excluded.current_track, "
                "last_success_at = excluded.last_success_at, "
                "last_change_at = excluded.last_change_at, "
                "consecutive_failures = 0, last_error = NULL",
                (station_key, track, now, changed_at),
            )
        else:
            failures = int(previous[2]) + 1 if previous else 1
            self.connection.execute(
                "INSERT INTO station_status "
                "(station_key, consecutive_failures, last_error) VALUES (?, ?, ?) "
                "ON CONFLICT(station_key) DO UPDATE SET "
                "consecutive_failures = excluded.consecutive_failures, "
                "last_error = excluded.last_error",
                (station_key, failures, error or "Нет данных"),
            )
        self.connection.commit()

    def station_statuses(self) -> list[tuple]:
        return self.connection.execute(
            "SELECT station_key, current_track, last_success_at, last_change_at, "
            "consecutive_failures, last_error FROM station_status"
        ).fetchall()

    def set_station_health(self, station_key: str, state: str) -> str | None:
        """Persist a health state and return the previous state when it changed."""
        row = self.connection.execute(
            "SELECT state FROM station_health WHERE station_key = ?",
            (station_key,),
        ).fetchone()
        previous = str(row[0]) if row else "unknown"
        if previous == state:
            return None
        self.connection.execute(
            "INSERT INTO station_health VALUES (?, ?, ?) "
            "ON CONFLICT(station_key) DO UPDATE SET "
            "state = excluded.state, changed_at = excluded.changed_at",
            (station_key, state, self._now()),
        )
        self.connection.commit()
        return previous

    def _ensure_settings(self, telegram_user_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO user_settings (telegram_user_id) VALUES (?)",
            (telegram_user_id,),
        )

    @staticmethod
    def _now() -> str:
        moscow_tz = timezone(timedelta(hours=3))
        return datetime.now(moscow_tz).isoformat(timespec="seconds")

    def close(self) -> None:
        self.connection.close()
