"""Persistent storage for Spotify OAuth states and user tokens."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredSpotifyToken:
    access_token: str
    refresh_token: str
    expires_at: int


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

    def connected_user_ids(self) -> list[int]:
        rows = self.connection.execute(
            "SELECT telegram_user_id FROM spotify_tokens"
        ).fetchall()
        return [int(row[0]) for row in rows]

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
                datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        self.connection.commit()
        return True

    def close(self) -> None:
        self.connection.close()
