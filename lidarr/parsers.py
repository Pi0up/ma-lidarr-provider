"""Parsers converting Lidarr v1 API payloads into Music Assistant media items."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import AlbumType, ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

ALBUM_TYPE_MAP: dict[str, AlbumType] = {
    "album": AlbumType.ALBUM,
    "single": AlbumType.SINGLE,
    "ep": AlbumType.EP,
    "compilation": AlbumType.COMPILATION,
    "live": AlbumType.LIVE,
    "soundtrack": AlbumType.SOUNDTRACK,
}


def _extract_images(
    raw_images: Any,
    provider_instance: str,
) -> UniqueList[MediaItemImage]:
    """
    Convert Lidarr ``images`` list into Music Assistant images.

    :param raw_images: Raw ``images`` field from a Lidarr artist or album payload.
    :param provider_instance: Provider instance ID owning the item.
    """
    images: UniqueList[MediaItemImage] = UniqueList()
    if not isinstance(raw_images, list):
        return images
    for img in raw_images:
        if not isinstance(img, dict):
            continue
        url = img.get("remoteUrl") or img.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        cover_type = str(img.get("coverType", "")).lower()
        img_type = ImageType.FANART if cover_type == "fanart" else ImageType.THUMB
        images.append(
            MediaItemImage(
                type=img_type,
                path=url,
                provider=provider_instance,
                remotely_accessible=True,
            )
        )
    return images


def _extract_year(release_date: Any) -> int | None:
    """
    Extract the 4-digit release year from an ISO date string.

    :param release_date: Raw Lidarr ``releaseDate`` value.
    """
    if isinstance(release_date, str) and len(release_date) >= 4:
        year_str = release_date[:4]
        if year_str.isdigit():
            return int(year_str)
    return None


def parse_artist(
    data: dict[str, Any],
    *,
    provider_domain: str,
    provider_instance: str,
) -> Artist | None:
    """
    Parse a Lidarr artist payload into a Music Assistant ``Artist``.

    :param data: Raw Lidarr artist dictionary.
    :param provider_domain: Provider domain string (``lidarr``).
    :param provider_instance: Provider instance ID.
    """
    foreign_id = data.get("foreignArtistId")
    name = data.get("artistName")
    if not isinstance(foreign_id, str) or not foreign_id or not isinstance(name, str) or not name:
        return None

    artist = Artist(
        item_id=foreign_id,
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=foreign_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
            )
        },
    )
    artist.add_external_id(ExternalID.MB_ARTIST, foreign_id)

    images = _extract_images(data.get("images"), provider_instance)
    if images:
        artist.metadata.images = images

    overview = data.get("overview")
    if isinstance(overview, str) and overview.strip():
        artist.metadata.description = overview.strip()

    genres = data.get("genres")
    if isinstance(genres, list):
        artist.metadata.genres = {str(g) for g in genres if isinstance(g, str) and g.strip()}

    return artist


def parse_album(
    data: dict[str, Any],
    *,
    provider_domain: str,
    provider_instance: str,
    version_tag: str = "",
) -> Album | None:
    """
    Parse a Lidarr album payload into a Music Assistant ``Album``.

    :param data: Raw Lidarr album dictionary.
    :param provider_domain: Provider domain string (``lidarr``).
    :param provider_instance: Provider instance ID.
    :param version_tag: Version label appended to the album (e.g. ``Lidarr Request``).
    """
    foreign_id = data.get("foreignAlbumId")
    title = data.get("title")
    if not isinstance(foreign_id, str) or not foreign_id or not isinstance(title, str) or not title:
        return None

    raw_album_type = str(data.get("albumType", "")).lower()
    album_type = ALBUM_TYPE_MAP.get(raw_album_type, AlbumType.UNKNOWN)

    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    raw_artist = data.get("artist")
    if isinstance(raw_artist, dict):
        parsed_artist = parse_artist(
            raw_artist,
            provider_domain=provider_domain,
            provider_instance=provider_instance,
        )
        if parsed_artist is not None:
            artists.append(parsed_artist)

    album = Album(
        item_id=foreign_id,
        provider=provider_instance,
        name=title,
        version=version_tag,
        year=_extract_year(data.get("releaseDate")),
        artists=artists,
        album_type=album_type,
        provider_mappings={
            ProviderMapping(
                item_id=foreign_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
            )
        },
    )
    album.add_external_id(ExternalID.MB_RELEASEGROUP, foreign_id)

    images = _extract_images(data.get("images"), provider_instance)
    if images:
        album.metadata.images = images

    overview = data.get("overview")
    if isinstance(overview, str) and overview.strip():
        album.metadata.description = overview.strip()

    genres = data.get("genres")
    if isinstance(genres, list):
        album.metadata.genres = {str(g) for g in genres if isinstance(g, str) and g.strip()}

    return album
