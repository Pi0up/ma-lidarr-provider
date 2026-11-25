"""
Lidarr music provider for Music Assistant.

Allows searching artists and albums directly through a Lidarr server and requesting missing
downloads by adding items to the Music Assistant library.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, override

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    SearchResults,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .client import (
    LidarrAuthError,
    LidarrClient,
    LidarrConnectionError,
    LidarrError,
    LidarrNotFoundError,
    LidarrRequestOptions,
)
from .constants import (
    ARTIST_MONITOR_MODES,
    BROWSE_MISSING,
    BROWSE_QUEUE,
    CONF_ACTION_LOAD_PROFILES,
    CONF_API_KEY,
    CONF_ARTIST_MONITOR,
    CONF_METADATA_PROFILE,
    CONF_POLL_INTERVAL,
    CONF_QUALITY_PROFILE,
    CONF_ROOT_FOLDER,
    CONF_SYNC_PROVIDER,
    CONF_TAG_ID,
    CONF_URL,
    CONF_VERSION_TAG,
    DEFAULT_ARTIST_MONITOR,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_VERSION_TAG,
    SYNC_PROVIDER_AUTO,
    SYNC_PROVIDER_NONE,
    TAG_NONE,
)
from .parsers import parse_album, parse_artist

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the Lidarr provider for one settings entry."""
    return LidarrProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return configuration entries for the Lidarr provider.

    When URL and API key are provided, queries Lidarr dynamically to populate root folders,
    quality profiles, metadata profiles, and tags.

    :param mass: Running Music Assistant instance.
    :param instance_id: Existing provider instance ID when editing settings.
    :param action: Optional UI action triggered by the user.
    :param values: Current unsaved form values.
    """
    current = values or {}
    raw_url = str(current.get(CONF_URL) or "").strip()
    raw_api_key = str(current.get(CONF_API_KEY) or "").strip()

    root_folder_options: list[ConfigValueOption] = []
    quality_options: list[ConfigValueOption] = []
    metadata_options: list[ConfigValueOption] = []
    tag_options: list[ConfigValueOption] = [ConfigValueOption("None", TAG_NONE)]

    if raw_url and raw_api_key:
        client = LidarrClient(mass.http_session, raw_url, raw_api_key)
        try:
            await client.get_system_status()
            root_folders, quality_profiles, metadata_profiles, tags = await asyncio.gather(
                client.get_root_folders(),
                client.get_quality_profiles(),
                client.get_metadata_profiles(),
                client.get_tags(),
            )
            root_folder_options = [
                ConfigValueOption(str(rf["path"]), str(rf["path"]))
                for rf in root_folders
                if isinstance(rf, dict) and rf.get("path")
            ]
            quality_options = [
                ConfigValueOption(str(qp["name"]), str(qp["id"]))
                for qp in quality_profiles
                if isinstance(qp, dict) and qp.get("id") is not None
            ]
            metadata_options = [
                ConfigValueOption(str(mp["name"]), str(mp["id"]))
                for mp in metadata_profiles
                if isinstance(mp, dict) and mp.get("id") is not None
            ]
            tag_options.extend(
                ConfigValueOption(str(tag["label"]), str(tag["id"]))
                for tag in tags
                if isinstance(tag, dict) and tag.get("id") is not None
            )
        except LidarrAuthError as err:
            if action == CONF_ACTION_LOAD_PROFILES:
                raise LoginFailed(str(err)) from err
        except LidarrError as err:
            if action == CONF_ACTION_LOAD_PROFILES:
                raise InvalidDataError(str(err)) from err

    sync_options: list[ConfigValueOption] = [
        ConfigValueOption("Auto (all local library providers)", SYNC_PROVIDER_AUTO),
        ConfigValueOption("Disabled", SYNC_PROVIDER_NONE),
    ]
    sync_options.extend(
        ConfigValueOption(prov.name, prov.instance_id)
        for prov in mass.music.providers
        if prov.domain != "lidarr" and not prov.is_streaming_provider
    )

    monitor_options = [ConfigValueOption(label, value) for value, label in ARTIST_MONITOR_MODES]

    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Lidarr URL",
            description="Base URL of your Lidarr server (e.g. http://192.168.1.50:8686).",
            required=True,
            value=raw_url or None,
        ),
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            label="API Key",
            description="Found in Lidarr under Settings > General > Security.",
            required=True,
            value=raw_api_key or None,
        ),
        ConfigEntry(
            key=CONF_ACTION_LOAD_PROFILES,
            type=ConfigEntryType.ACTION,
            label="Load Lidarr profiles",
            description="Connect to Lidarr and load root folders, quality profiles and tags.",
            action=CONF_ACTION_LOAD_PROFILES,
            action_label="Load profiles from Lidarr",
        ),
        ConfigEntry(
            key=CONF_ROOT_FOLDER,
            type=ConfigEntryType.STRING,
            label="Root Folder",
            description="Lidarr root folder where requested music will be stored.",
            options=root_folder_options,
            default_value=root_folder_options[0].value if root_folder_options else "",
            required=bool(root_folder_options),
        ),
        ConfigEntry(
            key=CONF_QUALITY_PROFILE,
            type=ConfigEntryType.STRING,
            label="Quality Profile",
            description="Lidarr quality profile used for newly added artists.",
            options=quality_options,
            default_value=quality_options[0].value if quality_options else "",
            required=bool(quality_options),
        ),
        ConfigEntry(
            key=CONF_METADATA_PROFILE,
            type=ConfigEntryType.STRING,
            label="Metadata Profile",
            description="Lidarr metadata profile used for newly added artists.",
            options=metadata_options,
            default_value=metadata_options[0].value if metadata_options else "",
            required=bool(metadata_options),
        ),
        ConfigEntry(
            key=CONF_ARTIST_MONITOR,
            type=ConfigEntryType.STRING,
            label="Artist Monitoring Mode",
            description="Which albums Lidarr monitors when you add a full artist.",
            options=monitor_options,
            default_value=DEFAULT_ARTIST_MONITOR,
            required=True,
        ),
        ConfigEntry(
            key=CONF_TAG_ID,
            type=ConfigEntryType.STRING,
            label="Lidarr Tag",
            description="Optional Lidarr tag applied to artists requested via Music Assistant.",
            options=tag_options,
            default_value=TAG_NONE,
            required=False,
        ),
        ConfigEntry(
            key=CONF_VERSION_TAG,
            type=ConfigEntryType.STRING,
            label="Album Version Label",
            description="Label shown on Lidarr search results to distinguish them in MA.",
            default_value=DEFAULT_VERSION_TAG,
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_SYNC_PROVIDER,
            type=ConfigEntryType.STRING,
            label="Library Provider to Rescan",
            description="Music provider to rescan automatically when Lidarr imports an album.",
            options=sync_options,
            default_value=SYNC_PROVIDER_AUTO,
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_POLL_INTERVAL,
            type=ConfigEntryType.INTEGER,
            label="Import Poll Interval (seconds)",
            description="How often to check Lidarr for finished imports (0 disables polling).",
            default_value=DEFAULT_POLL_INTERVAL,
            range=(0, 3600),
            required=False,
            advanced=True,
        ),
    ]
    return tuple(entries)


class LidarrProvider(MusicProvider):
    """Music Assistant provider bridging search and download requests to Lidarr."""

    client: LidarrClient
    _request_options: LidarrRequestOptions
    _version_tag: str
    _sync_provider: str
    _poll_interval: int
    _poll_task: asyncio.Task[None] | None = None
    _last_import_id: int | None = None

    def _get_setting(self, key: str) -> ConfigValueType | None:
        """Read a setting from setup_data first, falling back to runtime provider config."""
        setup_val = self.get_setup_value(key)
        if setup_val is not None and setup_val != "":
            return setup_val
        return self.config.get_value(key)

    async def handle_async_init(self) -> None:
        """Validate Lidarr connection, resolve defaults and start import watcher."""
        url = str(self._get_setting(CONF_URL) or "").strip()
        api_key = str(self._get_setting(CONF_API_KEY) or "").strip()
        if not url or not api_key:
            raise SetupFailedError("Lidarr URL and API key are required")

        self.client = LidarrClient(self.mass.http_session, url, api_key)
        try:
            await self.client.get_system_status()
        except LidarrAuthError as err:
            raise LoginFailed(str(err)) from err
        except LidarrConnectionError as err:
            raise SetupFailedError(str(err)) from err

        root_folder = await self._resolve_root_folder()
        quality_profile_id = await self._resolve_quality_profile_id()
        metadata_profile_id = await self._resolve_metadata_profile_id()
        artist_monitor = str(self._get_setting(CONF_ARTIST_MONITOR) or DEFAULT_ARTIST_MONITOR)

        raw_tag = str(self._get_setting(CONF_TAG_ID) or TAG_NONE)
        tag_ids = [int(raw_tag)] if raw_tag.isdigit() else []
        self._request_options = LidarrRequestOptions(
            root_folder_path=root_folder,
            quality_profile_id=quality_profile_id,
            metadata_profile_id=metadata_profile_id,
            artist_monitor=artist_monitor,
            tag_ids=tag_ids,
        )

        self._version_tag = str(self._get_setting(CONF_VERSION_TAG) or DEFAULT_VERSION_TAG)
        self._sync_provider = str(self._get_setting(CONF_SYNC_PROVIDER) or SYNC_PROVIDER_AUTO)
        raw_interval = self._get_setting(CONF_POLL_INTERVAL)
        self._poll_interval = (
            int(raw_interval)
            if isinstance(raw_interval, (int, float, str)) and str(raw_interval).isdigit()
            else DEFAULT_POLL_INTERVAL
        )

        if self._poll_interval > 0 and self._sync_provider != SYNC_PROVIDER_NONE:
            await self._seed_last_import_id()
            self._poll_task = self.mass.create_task(self._poll_imports())

    @override
    async def unload(self, is_removed: bool = False) -> None:
        """Stop background import polling when the provider is unloaded."""
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        self._poll_task = None

    @property
    def is_streaming_provider(self) -> bool:
        """Return True because Lidarr searches an external catalogue larger than local files."""
        return True

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search Lidarr for artists and albums matching the query.

        :param search_query: Search text entered by the user.
        :param media_types: Requested media types.
        :param limit: Maximum number of results per media type.
        """
        result = SearchResults()
        want_artists = MediaType.ARTIST in media_types
        want_albums = MediaType.ALBUM in media_types
        if not (want_artists or want_albums) or not search_query.strip():
            return result

        try:
            raw_items = await self.client.search(search_query.strip())
        except LidarrError as err:
            self.logger.warning("Lidarr search failed for %r: %s", search_query, err)
            return result

        artists: list[Artist] = []
        albums: list[Album] = []
        for entry in raw_items:
            raw_artist = entry.get("artist")
            if want_artists and isinstance(raw_artist, dict) and len(artists) < limit:
                parsed_artist = parse_artist(
                    raw_artist,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
                if parsed_artist is not None:
                    artists.append(parsed_artist)

            raw_album = entry.get("album")
            if want_albums and isinstance(raw_album, dict) and len(albums) < limit:
                parsed_album = parse_album(
                    raw_album,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    version_tag=self._version_tag,
                )
                if parsed_album is not None:
                    albums.append(parsed_album)

            if (not want_artists or len(artists) >= limit) and (
                not want_albums or len(albums) >= limit
            ):
                break

        result.artists = artists
        result.albums = albums
        return result

    @use_cache(3600)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Return metadata for a Lidarr artist by MusicBrainz artist UUID.

        :param prov_artist_id: MusicBrainz artist UUID.
        """
        try:
            results = await self.client.lookup_artist(f"lidarr:{prov_artist_id}")
        except LidarrError as err:
            raise MediaNotFoundError(f"Lidarr artist lookup failed: {err}") from err
        if results:
            parsed = parse_artist(
                results[0],
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
            if parsed is not None:
                return parsed
        raise MediaNotFoundError(f"Artist {prov_artist_id} not found in Lidarr")

    @use_cache(3600)
    async def get_album(self, prov_album_id: str) -> Album:
        """
        Return metadata for a Lidarr album by MusicBrainz release-group UUID.

        :param prov_album_id: MusicBrainz release-group UUID.
        """
        try:
            results = await self.client.lookup_album(f"lidarr:{prov_album_id}")
        except LidarrError as err:
            raise MediaNotFoundError(f"Lidarr album lookup failed: {err}") from err
        if results:
            parsed = parse_album(
                results[0],
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                version_tag=self._version_tag,
            )
            if parsed is not None:
                return parsed
        raise MediaNotFoundError(f"Album {prov_album_id} not found in Lidarr")

    async def library_add(self, item: MediaItemType) -> bool:
        """
        Request an album or artist download in Lidarr when added to the MA library.

        :param item: Media item added to the Music Assistant library.
        """
        if item.media_type == MediaType.ALBUM:
            try:
                await self.client.request_album(item.item_id, self._request_options)
            except LidarrNotFoundError as err:
                raise MediaNotFoundError(str(err)) from err
            except LidarrError:
                self.logger.exception("Failed to request album %s in Lidarr", item.name)
                return False
            self.logger.info("Requested album %r (%s) in Lidarr", item.name, item.item_id)
            return True

        if item.media_type == MediaType.ARTIST:
            try:
                await self.client.request_artist(item.item_id, self._request_options)
            except LidarrNotFoundError as err:
                raise MediaNotFoundError(str(err)) from err
            except LidarrError:
                self.logger.exception("Failed to request artist %s in Lidarr", item.name)
                return False
            self.logger.info("Requested artist %r (%s) in Lidarr", item.name, item.item_id)
            return True

        return False

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse Lidarr active downloads and monitored missing albums.

        :param path: Browse path prefixed with ``<instance_id>://``.
        """
        subpath = path.split("://", 1)[1] if "://" in path else ""
        if not subpath:
            return [
                BrowseFolder(
                    item_id=BROWSE_QUEUE,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{BROWSE_QUEUE}",
                    name="Downloading (Queue)",
                ),
                BrowseFolder(
                    item_id=BROWSE_MISSING,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{BROWSE_MISSING}",
                    name="Missing / Requested Albums",
                ),
            ]

        if subpath == BROWSE_QUEUE:
            return await self._browse_queue()
        if subpath == BROWSE_MISSING:
            return await self._browse_missing()
        return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Refuse direct playback without triggering an unintended Lidarr download.

        :param item_id: Provider item ID.
        :param media_type: Media type requested for playback.
        """
        raise MediaNotFoundError(
            "Lidarr items cannot be streamed directly. Add the album or artist to your library "
            "to request a download in Lidarr."
        )

    async def _browse_queue(self) -> list[Album]:
        """Return albums currently in the Lidarr download queue."""
        try:
            records = await self.client.get_queue()
        except LidarrError as err:
            self.logger.warning("Failed to fetch Lidarr queue: %s", err)
            return []

        items: list[Album] = []
        for record in records:
            raw_album = record.get("album")
            if not isinstance(raw_album, dict):
                continue
            album_data = dict(raw_album)
            if "artist" not in album_data and isinstance(record.get("artist"), dict):
                album_data["artist"] = record["artist"]
            status = str(record.get("status", "queued")).lower()
            size = float(record.get("size") or 0)
            sizeleft = float(record.get("sizeleft") or 0)
            if size > 0:
                pct = max(0, min(100, round((1.0 - sizeleft / size) * 100)))
                label = f"Lidarr: {status} ({pct}%)"
            else:
                label = f"Lidarr: {status}"
            parsed = parse_album(
                album_data,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                version_tag=label,
            )
            if parsed is not None:
                items.append(parsed)
        return items

    async def _browse_missing(self) -> list[Album]:
        """Return monitored missing albums from Lidarr."""
        try:
            records = await self.client.get_wanted_missing()
        except LidarrError as err:
            self.logger.warning("Failed to fetch Lidarr missing albums: %s", err)
            return []

        items: list[Album] = []
        for record in records:
            parsed = parse_album(
                record,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                version_tag="Lidarr: missing",
            )
            if parsed is not None:
                items.append(parsed)
        return items

    async def _resolve_root_folder(self) -> str:
        """Return configured root folder or fall back to the first Lidarr root folder."""
        configured = str(self._get_setting(CONF_ROOT_FOLDER) or "").strip()
        if configured:
            return configured
        folders = await self.client.get_root_folders()
        if folders and folders[0].get("path"):
            return str(folders[0]["path"])
        raise SetupFailedError("No root folder configured in Lidarr")

    async def _resolve_quality_profile_id(self) -> int:
        """Return configured quality profile ID or fall back to the first profile in Lidarr."""
        configured = str(self._get_setting(CONF_QUALITY_PROFILE) or "").strip()
        if configured.isdigit():
            return int(configured)
        profiles = await self.client.get_quality_profiles()
        if profiles and profiles[0].get("id") is not None:
            return int(profiles[0]["id"])
        raise SetupFailedError("No quality profile configured in Lidarr")

    async def _resolve_metadata_profile_id(self) -> int:
        """Return configured metadata profile ID or fall back to the first profile in Lidarr."""
        configured = str(self._get_setting(CONF_METADATA_PROFILE) or "").strip()
        if configured.isdigit():
            return int(configured)
        profiles = await self.client.get_metadata_profiles()
        if profiles and profiles[0].get("id") is not None:
            return int(profiles[0]["id"])
        raise SetupFailedError("No metadata profile configured in Lidarr")

    async def _seed_last_import_id(self) -> None:
        """Record the latest Lidarr import history ID at startup."""
        try:
            imports = await self.client.get_recent_imports(page_size=1)
        except LidarrError:
            return
        if imports and isinstance(imports[0], dict) and imports[0].get("id") is not None:
            self._last_import_id = int(imports[0]["id"])

    async def _poll_imports(self) -> None:
        """Periodically poll Lidarr history and trigger a local MA rescan on new imports."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                imports = await self.client.get_recent_imports(page_size=5)
            except LidarrError as err:
                self.logger.debug("Lidarr import poll skipped: %s", err)
                continue
            if not imports or not isinstance(imports[0], dict):
                continue
            latest_id = imports[0].get("id")
            if latest_id is None:
                continue
            latest_int = int(latest_id)
            if self._last_import_id is None:
                self._last_import_id = latest_int
                continue
            if latest_int > self._last_import_id:
                self._last_import_id = latest_int
                await self._trigger_local_rescan()

    async def _trigger_local_rescan(self) -> None:
        """Trigger a library sync on the configured local Music Assistant provider(s)."""
        if self._sync_provider == SYNC_PROVIDER_NONE:
            return
        if self._sync_provider == SYNC_PROVIDER_AUTO:
            targets = [
                prov.instance_id
                for prov in self.mass.music.providers
                if prov.domain != self.domain and not prov.is_streaming_provider
            ]
        else:
            targets = [self._sync_provider]

        if not targets:
            return
        self.logger.info(
            "Lidarr imported new audio; triggering Music Assistant rescan for %s",
            targets,
        )
        await self.mass.music.start_sync(providers=targets)
