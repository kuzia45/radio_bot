"""Safe Yandex Music device authorization and liked-track access."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from yandex_music import ClientAsync, DeviceCode
from yandex_music.exceptions import YandexMusicError as LibraryYandexMusicError

from src.spotify import SpotifyTrack


class YandexMusicError(RuntimeError):
    """Yandex Music authorization or library access failed."""


async def authorize_yandex_music(
    on_code: Callable[[DeviceCode], Awaitable[None] | None],
) -> tuple[str, str | None, int | None]:
    """Authorize without collecting a password and return renewable tokens."""
    try:
        token = await ClientAsync().device_auth(on_code=on_code)
    except LibraryYandexMusicError as exc:
        raise YandexMusicError("Не удалось подключить Яндекс Музыку") from exc
    return token.access_token, token.refresh_token, token.expires_in


async def fetch_yandex_liked_tracks(
    access_token: str, limit: int | None = None
) -> list[SpotifyTrack]:
    """Return liked Yandex Music tracks, newest first."""
    try:
        client = await ClientAsync(access_token).init()
        liked = await client.users_likes_tracks()
        shorts = list(liked or [])
        if limit is not None:
            shorts = shorts[:limit]

        result: list[SpotifyTrack] = []
        for offset in range(0, len(shorts), 50):
            batch = shorts[offset : offset + 50]
            identifiers = [
                f"{item.id}:{item.album_id}" if item.album_id else str(item.id)
                for item in batch
            ]
            tracks = await client.tracks(identifiers)
            by_id = {str(track.id): track for track in tracks}
            for item in batch:
                track = by_id.get(str(item.id))
                if track is None or not track.title:
                    continue
                artists = ", ".join(
                    artist.name for artist in track.artists if artist.name
                )
                if artists:
                    result.append(SpotifyTrack(artist=artists, title=track.title))
        return result
    except LibraryYandexMusicError as exc:
        raise YandexMusicError("Яндекс Музыка временно недоступна") from exc
