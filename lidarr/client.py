"""Async HTTP client for the Lidarr v1 REST API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .constants import (
    API_PREFIX,
    DEFAULT_ARTIST_MONITOR,
    DEFAULT_TIMEOUT,
    LIDARR_EVENT_TRACK_IMPORTED,
)


@dataclass(frozen=True)
class LidarrRequestOptions:
    """Target Lidarr settings used when adding or monitoring an album or artist."""

    root_folder_path: str
    quality_profile_id: int
    metadata_profile_id: int
    artist_monitor: str = DEFAULT_ARTIST_MONITOR
    tag_ids: list[int] = field(default_factory=list)


class LidarrError(Exception):
    """Base error raised by the Lidarr API client."""


class LidarrAuthError(LidarrError):
    """Raised when Lidarr rejects the configured API key."""


class LidarrConnectionError(LidarrError):
    """Raised when Lidarr cannot be reached or returns a server error."""


class LidarrNotFoundError(LidarrError):
    """Raised when a requested Lidarr resource or lookup item is not found."""


class LidarrClient:
    """Async client communicating with a Lidarr server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        timeout: int = DEFAULT_TIMEOUT,
        rate_limit_delay: float = 0.1,
    ) -> None:
        """
        Initialize the Lidarr API client.

        :param session: Shared aiohttp client session.
        :param base_url: Base URL of the Lidarr instance (e.g. ``http://lidarr:8686``).
        :param api_key: Lidarr API key.
        :param timeout: Per-request timeout in seconds.
        :param rate_limit_delay: Minimum delay in seconds between consecutive API requests.
        """
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._rate_limit_delay = rate_limit_delay
        self._lock = asyncio.Lock()
        self._last_request_ts = 0.0

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """
        Send an authenticated request to the Lidarr v1 API.

        :param method: HTTP method (GET, POST, PUT, DELETE).
        :param endpoint: Endpoint path relative to ``/api/v1`` (e.g. ``/search``).
        :param params: Optional query string parameters.
        :param json_body: Optional JSON request body.
        """
        url = f"{self._base_url}{API_PREFIX}{endpoint}"
        headers = {
            "X-Api-Key": self._api_key,
            "Accept": "application/json",
        }
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._rate_limit_delay - (now - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = asyncio.get_running_loop().time()

            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                ) as response:
                    if response.status in (401, 403):
                        raise LidarrAuthError("Invalid Lidarr API key or unauthorized access")
                    if response.status == 404:
                        raise LidarrNotFoundError(f"Lidarr resource not found: {endpoint}")
                    if response.status == 429:
                        retry_after = float(response.headers.get("Retry-After", "2"))
                        await asyncio.sleep(min(retry_after, 10.0))
                        raise LidarrConnectionError("Lidarr rate limit exceeded")
                    if response.status >= 400:
                        text = await response.text()
                        raise LidarrConnectionError(
                            f"Lidarr API error {response.status} on {endpoint}: {text[:200]}"
                        )
                    if response.status == 204:
                        return None
                    return await response.json()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise LidarrConnectionError(f"Unable to reach Lidarr at {url}: {err}") from err

    async def get_system_status(self) -> dict[str, Any]:
        """Return Lidarr system status to verify connectivity and authentication."""
        data = await self._request("GET", "/system/status")
        if not isinstance(data, dict):
            raise LidarrConnectionError("Unexpected response from Lidarr /system/status")
        return data

    async def get_root_folders(self) -> list[dict[str, Any]]:
        """Return configured Lidarr root folders."""
        data = await self._request("GET", "/rootfolder")
        return data if isinstance(data, list) else []

    async def get_quality_profiles(self) -> list[dict[str, Any]]:
        """Return configured Lidarr quality profiles."""
        data = await self._request("GET", "/qualityprofile")
        return data if isinstance(data, list) else []

    async def get_metadata_profiles(self) -> list[dict[str, Any]]:
        """Return configured Lidarr metadata profiles."""
        data = await self._request("GET", "/metadataprofile")
        return data if isinstance(data, list) else []

    async def get_tags(self) -> list[dict[str, Any]]:
        """Return configured Lidarr tags."""
        data = await self._request("GET", "/tag")
        return data if isinstance(data, list) else []

    async def search(self, term: str) -> list[dict[str, Any]]:
        """
        Search Lidarr for artists and albums matching the query.

        :param term: Search query string.
        """
        data = await self._request("GET", "/search", params={"term": term})
        return data if isinstance(data, list) else []

    async def lookup_artist(self, term: str) -> list[dict[str, Any]]:
        """
        Look up an artist via Lidarr metadata search.

        :param term: Artist name or ``lidarr:<foreignArtistId>``.
        """
        data = await self._request("GET", "/artist/lookup", params={"term": term})
        return data if isinstance(data, list) else []

    async def lookup_album(self, term: str) -> list[dict[str, Any]]:
        """
        Look up an album via Lidarr metadata search.

        :param term: Album title or ``lidarr:<foreignAlbumId>``.
        """
        data = await self._request("GET", "/album/lookup", params={"term": term})
        return data if isinstance(data, list) else []

    async def get_artist_by_foreign_id(self, foreign_artist_id: str) -> dict[str, Any] | None:
        """
        Return the existing Lidarr artist matching the given MusicBrainz artist ID, or None.

        :param foreign_artist_id: MusicBrainz artist UUID.
        """
        data = await self._request("GET", "/artist", params={"mbId": foreign_artist_id})
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return first
        return None

    async def get_albums(
        self,
        *,
        artist_id: int | None = None,
        foreign_album_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return existing Lidarr albums filtered by artist ID or foreign album ID.

        :param artist_id: Optional internal Lidarr artist ID.
        :param foreign_album_id: Optional MusicBrainz release-group UUID.
        """
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        if foreign_album_id is not None:
            params["foreignAlbumId"] = foreign_album_id
        data = await self._request("GET", "/album", params=params or None)
        return data if isinstance(data, list) else []

    async def add_artist(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Add a new artist to Lidarr.

        :param payload: Full Lidarr artist creation payload.
        """
        data = await self._request("POST", "/artist", json_body=payload)
        if not isinstance(data, dict):
            raise LidarrError("Unexpected response when adding artist to Lidarr")
        return data

    async def update_artist(self, artist_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Update an existing artist in Lidarr.

        :param artist_id: Internal Lidarr artist ID.
        :param payload: Updated Lidarr artist object.
        """
        data = await self._request("PUT", f"/artist/{artist_id}", json_body=payload)
        if not isinstance(data, dict):
            raise LidarrError("Unexpected response when updating artist in Lidarr")
        return data

    async def set_albums_monitored(
        self, album_ids: list[int], *, monitored: bool = True
    ) -> list[dict[str, Any]]:
        """
        Set the monitored flag on one or more existing Lidarr albums.

        :param album_ids: Internal Lidarr album IDs.
        :param monitored: Desired monitored state.
        """
        data = await self._request(
            "PUT",
            "/album/monitor",
            json_body={"albumIds": album_ids, "monitored": monitored},
        )
        return data if isinstance(data, list) else []

    async def trigger_album_search(self, album_ids: list[int]) -> dict[str, Any]:
        """
        Trigger an immediate ``AlbumSearch`` command in Lidarr.

        :param album_ids: Internal Lidarr album IDs to search for.
        """
        data = await self._request(
            "POST",
            "/command",
            json_body={"name": "AlbumSearch", "albumIds": album_ids},
        )
        return data if isinstance(data, dict) else {}

    async def trigger_artist_search(self, artist_id: int) -> dict[str, Any]:
        """
        Trigger an immediate ``ArtistSearch`` command in Lidarr.

        :param artist_id: Internal Lidarr artist ID to search for.
        """
        data = await self._request(
            "POST",
            "/command",
            json_body={"name": "ArtistSearch", "artistId": artist_id},
        )
        return data if isinstance(data, dict) else {}

    async def get_queue(self, page: int = 1, page_size: int = 50) -> list[dict[str, Any]]:
        """
        Return active download queue records from Lidarr.

        :param page: Page number (1-indexed).
        :param page_size: Number of records per page.
        """
        data = await self._request(
            "GET",
            "/queue",
            params={
                "page": page,
                "pageSize": page_size,
                "includeArtist": "true",
                "includeAlbum": "true",
            },
        )
        if isinstance(data, dict):
            records = data.get("records", [])
            return records if isinstance(records, list) else []
        return []

    async def get_wanted_missing(self, page: int = 1, page_size: int = 50) -> list[dict[str, Any]]:
        """
        Return monitored missing albums from Lidarr.

        :param page: Page number (1-indexed).
        :param page_size: Number of records per page.
        """
        data = await self._request(
            "GET",
            "/wanted/missing",
            params={
                "page": page,
                "pageSize": page_size,
                "includeArtist": "true",
                "monitored": "true",
            },
        )
        if isinstance(data, dict):
            records = data.get("records", [])
            return records if isinstance(records, list) else []
        return []

    async def get_recent_imports(self, page_size: int = 20) -> list[dict[str, Any]]:
        """
        Return the most recent track import history events from Lidarr.

        :param page_size: Maximum number of history entries to fetch.
        """
        data = await self._request(
            "GET",
            "/history",
            params={
                "page": 1,
                "pageSize": page_size,
                "eventType": LIDARR_EVENT_TRACK_IMPORTED,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        if isinstance(data, dict):
            records = data.get("records", [])
            return records if isinstance(records, list) else []
        return []

    async def request_album(
        self,
        foreign_album_id: str,
        options: LidarrRequestOptions,
    ) -> dict[str, Any]:
        """
        Ensure an album is monitored in Lidarr and trigger an immediate AlbumSearch.

        If the artist is not yet in Lidarr, the artist is added with ``monitor="none"`` and
        ``albumsToMonitor=[foreign_album_id]`` so only the requested album is downloaded.

        :param foreign_album_id: MusicBrainz release-group UUID of the album.
        :param options: Lidarr target folder, profiles and tags.
        """
        existing_albums = await self.get_albums(foreign_album_id=foreign_album_id)
        if existing_albums:
            album = existing_albums[0]
            album_id = int(album["id"])
            if not album.get("monitored", False):
                await self.set_albums_monitored([album_id], monitored=True)
            await self.trigger_album_search([album_id])
            return album

        lookup_results = await self.lookup_album(f"lidarr:{foreign_album_id}")
        if not lookup_results:
            raise LidarrNotFoundError(f"Album {foreign_album_id} not found in Lidarr lookup")

        album_meta = lookup_results[0]
        artist_meta = album_meta.get("artist")
        foreign_artist_id = (
            artist_meta.get("foreignArtistId") if isinstance(artist_meta, dict) else None
        )
        if not foreign_artist_id:
            raise LidarrNotFoundError(
                f"Album {foreign_album_id} has no associated artist in Lidarr metadata"
            )

        existing_artist = await self.get_artist_by_foreign_id(str(foreign_artist_id))
        if existing_artist is None:
            artist_lookup = await self.lookup_artist(f"lidarr:{foreign_artist_id}")
            if not artist_lookup:
                raise LidarrNotFoundError(f"Artist {foreign_artist_id} not found in Lidarr lookup")
            artist_payload = dict(artist_lookup[0])
            artist_payload.update(
                {
                    "rootFolderPath": options.root_folder_path,
                    "qualityProfileId": options.quality_profile_id,
                    "metadataProfileId": options.metadata_profile_id,
                    "monitored": True,
                    "tags": list(options.tag_ids),
                    "addOptions": {
                        "monitor": "none",
                        "albumsToMonitor": [foreign_album_id],
                        "searchForMissingAlbums": True,
                    },
                }
            )
            return await self.add_artist(artist_payload)

        # Artist exists in Lidarr; locate the album under this artist, monitor it, and search.
        artist_albums = await self.get_albums(artist_id=int(existing_artist["id"]))
        for album in artist_albums:
            if album.get("foreignAlbumId") == foreign_album_id:
                album_id = int(album["id"])
                if not album.get("monitored", False):
                    await self.set_albums_monitored([album_id], monitored=True)
                await self.trigger_album_search([album_id])
                return album

        raise LidarrNotFoundError(
            f"Album {foreign_album_id} is not available under existing Lidarr artist "
            f"{existing_artist.get('artistName', foreign_artist_id)}"
        )

    async def request_artist(
        self,
        foreign_artist_id: str,
        options: LidarrRequestOptions,
    ) -> dict[str, Any]:
        """
        Ensure an artist is monitored in Lidarr and trigger an immediate ArtistSearch.

        :param foreign_artist_id: MusicBrainz artist UUID.
        :param options: Lidarr target folder, profiles, monitoring mode and tags.
        """
        existing_artist = await self.get_artist_by_foreign_id(foreign_artist_id)
        if existing_artist is not None:
            artist_id = int(existing_artist["id"])
            if not existing_artist.get("monitored", False):
                updated = dict(existing_artist)
                updated["monitored"] = True
                existing_artist = await self.update_artist(artist_id, updated)
            await self.trigger_artist_search(artist_id)
            return existing_artist

        artist_lookup = await self.lookup_artist(f"lidarr:{foreign_artist_id}")
        if not artist_lookup:
            raise LidarrNotFoundError(f"Artist {foreign_artist_id} not found in Lidarr lookup")

        artist_payload = dict(artist_lookup[0])
        artist_payload.update(
            {
                "rootFolderPath": options.root_folder_path,
                "qualityProfileId": options.quality_profile_id,
                "metadataProfileId": options.metadata_profile_id,
                "monitored": True,
                "tags": list(options.tag_ids),
                "addOptions": {
                    "monitor": options.artist_monitor,
                    "searchForMissingAlbums": True,
                },
            }
        )
        return await self.add_artist(artist_payload)
