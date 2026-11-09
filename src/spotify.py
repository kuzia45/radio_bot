"""Spotify configuration, authorization URL generation and track matching."""

from __future__ import annotations

import logging
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

import aiohttp

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_SAVED_TRACKS_URL = "https://api.spotify.com/v1/me/tracks"
SPOTIFY_SCOPES = "user-library-read user-library-modify"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpotifyConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    artist: str
    title: str


@dataclass(frozen=True, slots=True)
class SpotifyOAuthToken:
    access_token: str
    refresh_token: str
    expires_at: int


class SpotifyError(RuntimeError):
    """Spotify could not return the user's saved tracks."""


async def save_track_to_spotify(
    session: aiohttp.ClientSession, access_token: str, query: str
) -> SpotifyTrack:
    """Find the closest Spotify track and add it to the user's library."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with session.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": 1},
            headers=headers,
        ) as response:
            await _raise_spotify_api_error(response)
            payload = await response.json(content_type=None)
        items = payload.get("tracks", {}).get("items", [])
        if not items:
            raise SpotifyError("Spotify could not find this track")
        item = items[0]
        uri = str(item["uri"])
        artist = ", ".join(value["name"] for value in item.get("artists", []))
        title = str(item["name"])
        async with session.put(
            "https://api.spotify.com/v1/me/library",
            json={"uris": [uri]},
            headers=headers,
        ) as response:
            await _raise_spotify_api_error(response)
        return SpotifyTrack(artist=artist, title=title)
    except (aiohttp.ClientError, KeyError, TypeError, ValueError) as exc:
        logger.warning("SPOTIFY SAVE ERROR | type=%s", type(exc).__name__)
        raise SpotifyError("Spotify could not save this track") from exc


async def _raise_spotify_api_error(response: aiohttp.ClientResponse) -> None:
    """Turn an unsuccessful Spotify API response into a useful safe error."""
    if response.status < 400:
        return
    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    detail = error.get("message", "") if isinstance(error, dict) else ""
    reason = error.get("reason", "") if isinstance(error, dict) else ""
    logger.warning(
        "SPOTIFY API ERROR | status=%s | reason=%s | detail=%s",
        response.status,
        reason or "unknown",
        detail or "unknown",
    )
    if response.status == 401:
        raise SpotifyError("Spotify authorization has expired")
    if response.status == 403:
        raise SpotifyError(
            "Spotify denied access (403). Check Premium and the app allowlist"
        )
    if response.status == 429:
        if reason == "QUOTA_EXCEEDED":
            raise SpotifyError("Spotify API quota has been exceeded (429)")
        raise SpotifyError("Too many requests to Spotify (429). Try again later")
    if response.status >= 500:
        raise SpotifyError(f"Spotify service error ({response.status})")
    raise SpotifyError(f"Spotify rejected the request ({response.status})")


async def _raise_spotify_token_error(response: aiohttp.ClientResponse) -> None:
    """Describe OAuth token errors without logging credentials or tokens."""
    if response.status < 400:
        return
    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        payload = {}
    code = str(payload.get("error", "")) if isinstance(payload, dict) else ""
    logger.warning(
        "SPOTIFY OAUTH ERROR | status=%s | code=%s",
        response.status,
        code or "unknown",
    )
    if code == "invalid_client":
        raise SpotifyError("Spotify Client ID or Client Secret is invalid")
    if code == "invalid_grant":
        raise SpotifyError("Spotify authorization has expired; connect it again")
    if response.status == 429:
        raise SpotifyError("Too many authorization requests to Spotify (429)")
    if response.status >= 500:
        raise SpotifyError(f"Spotify authorization service error ({response.status})")
    raise SpotifyError(f"Spotify rejected the authorization ({response.status})")


async def fetch_recent_liked_tracks(
    session: aiohttp.ClientSession, access_token: str, limit: int = 10
) -> list[SpotifyTrack]:
    """Return the most recently liked Spotify tracks, newest first."""
    try:
        async with session.get(
            SPOTIFY_SAVED_TRACKS_URL,
            params={"limit": limit, "offset": 0},
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            await _raise_spotify_api_error(response)
            payload: dict[str, Any] = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        logger.warning("SPOTIFY API NETWORK ERROR | type=%s", type(exc).__name__)
        raise SpotifyError("Spotify is temporarily unavailable") from exc

    tracks: list[SpotifyTrack] = []
    for item in payload.get("items", []):
        track = item.get("track") or {}
        artists = ", ".join(
            artist.get("name", "") for artist in track.get("artists", [])
        ).strip(", ")
        title = str(track.get("name", "")).strip()
        if artists and title:
            tracks.append(SpotifyTrack(artist=artists, title=title))
    return tracks


async def fetch_all_liked_tracks(
    session: aiohttp.ClientSession, access_token: str
) -> list[SpotifyTrack]:
    """Return the complete saved-track library using Spotify pagination."""
    tracks: list[SpotifyTrack] = []
    offset = 0
    limit = 50
    while True:
        try:
            async with session.get(
                SPOTIFY_SAVED_TRACKS_URL,
                params={"limit": limit, "offset": offset},
                headers={"Authorization": f"Bearer {access_token}"},
            ) as response:
                await _raise_spotify_api_error(response)
                payload: dict[str, Any] = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            logger.warning("SPOTIFY API NETWORK ERROR | type=%s", type(exc).__name__)
            raise SpotifyError("Spotify is temporarily unavailable") from exc

        items = payload.get("items", [])
        for item in items:
            track = item.get("track") or {}
            artists = ", ".join(
                artist.get("name", "") for artist in track.get("artists", [])
            ).strip(", ")
            title = str(track.get("name", "")).strip()
            if artists and title:
                tracks.append(SpotifyTrack(artist=artists, title=title))
        if len(items) < limit or not payload.get("next"):
            return tracks
        offset += limit


def create_authorization_url(
    config: SpotifyConfig, telegram_user_id: int
) -> tuple[str, str]:
    """Build a Spotify consent URL and return its state for validation."""
    state = f"{telegram_user_id}:{secrets.token_urlsafe(24)}"
    query = urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "redirect_uri": config.redirect_uri,
            "scope": SPOTIFY_SCOPES,
            "state": state,
            "show_dialog": "true",
        }
    )
    return f"{SPOTIFY_AUTHORIZE_URL}?{query}", state


def build_authorization_url(config: SpotifyConfig, telegram_user_id: int) -> str:
    """Build a Spotify consent URL."""
    return create_authorization_url(config, telegram_user_id)[0]


async def exchange_authorization_code(
    session: aiohttp.ClientSession, config: SpotifyConfig, code: str
) -> SpotifyOAuthToken:
    """Exchange a callback code for renewable user tokens."""
    payload = await _request_token(
        session,
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
        },
    )
    return _parse_oauth_token(payload)


async def refresh_access_token(
    session: aiohttp.ClientSession,
    config: SpotifyConfig,
    refresh_token: str,
) -> SpotifyOAuthToken:
    """Refresh an expired access token while retaining its refresh token."""
    payload = await _request_token(
        session,
        config,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    payload.setdefault("refresh_token", refresh_token)
    return _parse_oauth_token(payload)


async def _request_token(
    session: aiohttp.ClientSession,
    config: SpotifyConfig,
    data: dict[str, str],
) -> dict[str, Any]:
    try:
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data=data,
            auth=aiohttp.BasicAuth(config.client_id, config.client_secret),
        ) as response:
            await _raise_spotify_token_error(response)
            return await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as exc:
        logger.warning("SPOTIFY OAUTH NETWORK ERROR | type=%s", type(exc).__name__)
        raise SpotifyError("Spotify authorization is temporarily unavailable") from exc


def _parse_oauth_token(payload: dict[str, Any]) -> SpotifyOAuthToken:
    try:
        return SpotifyOAuthToken(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            expires_at=int(time.time()) + int(payload.get("expires_in", 3600)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SpotifyError(
            "Spotify returned an invalid authorization response"
        ) from exc


def normalize_track(value: str) -> str:
    """Normalize radio and Spotify titles for conservative comparison."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\([^)]*(?:remix|remaster|radio edit|version)[^)]*\)", "", value)
    value = re.sub(r"\[[^]]*(?:remix|remaster|radio edit|version)[^]]*\]", "", value)
    value = value.replace("ё", "е").replace("&", " and ")
    return re.sub(r"[^\w]+", " ", value).strip()


def split_radio_track(value: str) -> tuple[str, str] | None:
    """Split the conventional 'artist - title' value returned by radio sources."""
    artist, separator, title = value.partition(" - ")
    if not separator or not artist.strip() or not title.strip():
        return None
    return artist.strip(), title.strip()


def tracks_match(radio_value: str, spotify_artist: str, spotify_title: str) -> bool:
    """Compare artist and title, allowing only small spelling differences."""
    parsed = split_radio_track(radio_value)
    if parsed is None:
        return False
    radio_artist, radio_title = parsed
    normalized_radio_artist = normalize_track(radio_artist)
    normalized_spotify_artist = normalize_track(spotify_artist)
    normalized_radio_title = normalize_track(radio_title)
    normalized_spotify_title = normalize_track(spotify_title)

    artist_matches = _values_match(
        normalized_radio_artist, normalized_spotify_artist, threshold=0.84
    )
    title_matches = _values_match(
        normalized_radio_title, normalized_spotify_title, threshold=0.9
    )
    return artist_matches and title_matches


def _values_match(left: str, right: str, threshold: float) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True

    # Short strings can look deceptively similar while naming different tracks.
    compact_left = left.replace(" ", "")
    compact_right = right.replace(" ", "")
    if min(len(compact_left), len(compact_right)) < 6:
        return False

    return SequenceMatcher(None, left, right).ratio() >= threshold
