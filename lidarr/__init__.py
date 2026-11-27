"""
Lidarr music provider for Music Assistant.

Allows searching artists and albums directly through a Lidarr server and requesting missing
downloads by adding items to the Music Assistant library.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, override

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ExternalID,
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
    Track,
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
    REQUEST_MAX_WAIT_SECONDS,
    REQUEST_POLL_INTERVAL,
    SUBSONIC_SCAN_MAX_WAIT_SECONDS,
    SUBSONIC_SCAN_POLL_INTERVAL,
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
    ProviderFeature.ALBUM_BY_EXTERNAL_ID,
    ProviderFeature.ARTIST_ALBUMS,
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
    _completing_ids: set[str]

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
        self._completing_ids = set()
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

        self.mass.create_task(self._cleanup_completed_requests())

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
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """
        Return all albums for a Lidarr artist by MusicBrainz artist UUID.

        :param prov_artist_id: MusicBrainz artist UUID.
        """
        try:
            raw_albums = await self.client.get_artist_albums(prov_artist_id)
        except LidarrError as err:
            self.logger.warning(
                "Failed to fetch Lidarr albums for artist %s: %s", prov_artist_id, err
            )
            return []

        albums: list[Album] = []
        for raw_album in raw_albums:
            parsed = parse_album(
                raw_album,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                version_tag=self._version_tag,
            )
            if parsed is not None:
                albums.append(parsed)
        return albums

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

    async def get_album_by_external_id(
        self, external_id: str, external_id_type: ExternalID
    ) -> Album | None:
        """
        Resolve an album from MusicBrainz (release-group, release, or barcode) into a Lidarr album.

        Enables right-click "Add to library" on albums shown in the "from MusicBrainz" artist
        discography section in Music Assistant.

        :param external_id: External identifier value.
        :param external_id_type: External identifier type.
        """
        rg_id: str | None = None
        if external_id_type == ExternalID.MB_RELEASEGROUP:
            rg_id = external_id
        elif external_id_type in (ExternalID.BARCODE, ExternalID.MB_ALBUM):
            musicbrainz: Any = self.mass.get_provider("musicbrainz")
            if musicbrainz is None:
                return None
            try:
                if external_id_type == ExternalID.MB_ALBUM:
                    release = await musicbrainz.get_release_details(external_id)
                    rg = getattr(release, "release_group", None)
                    rg_id = getattr(rg, "id", None)
                else:
                    releases = await musicbrainz.get_releases_by_barcode(external_id)
                    for release in releases:
                        rg = getattr(release, "release_group", None)
                        if found_id := getattr(rg, "id", None):
                            rg_id = str(found_id)
                            break
            except Exception as err:  # noqa: BLE001
                self.logger.debug(
                    "MusicBrainz resolution failed for %s=%s: %s",
                    external_id_type.value,
                    external_id,
                    err,
                )
                return None

        if not rg_id:
            return None
        try:
            return await self.get_album(rg_id)
        except MediaNotFoundError:
            return None

    async def get_album_tracks(
        self,
        prov_album_id: str,
        in_library_only: bool = False,  # noqa: FBT001, FBT002
    ) -> list[Track]:
        """Return an empty track list for Lidarr request placeholders."""
        return []

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Cancel a Lidarr download request when the user removes the item from the MA library.

        :param prov_item_id: MusicBrainz album or artist UUID.
        :param media_type: ``MediaType.ALBUM`` or ``MediaType.ARTIST``.
        """
        if prov_item_id in self._completing_ids:
            return True
        task_id = f"lidarr_request_{media_type.value}_{prov_item_id}"
        with contextlib.suppress(Exception):
            self.mass.tasks.cancel_task(task_id)

        try:
            if media_type == MediaType.ALBUM:
                await self.client.cancel_album_request(prov_item_id)
                self.logger.info("Cancelled Lidarr album request (%s)", prov_item_id)
                return True
            if media_type == MediaType.ARTIST:
                await self.client.cancel_artist_request(prov_item_id)
                self.logger.info("Cancelled Lidarr artist request (%s)", prov_item_id)
                return True
        except LidarrError:
            self.logger.exception(
                "Failed to cancel Lidarr %s request (%s)",
                media_type.value,
                prov_item_id,
            )
            return False
        return False

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
            self._start_request_tracker(item.item_id, item.name, MediaType.ALBUM)
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
            self._start_request_tracker(item.item_id, item.name, MediaType.ARTIST)
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

    def _start_request_tracker(
        self,
        item_id: str,
        item_name: str,
        media_type: MediaType,
    ) -> None:
        """
        Launch a Music Assistant background task that tracks a Lidarr request until completion.

        :param item_id: MusicBrainz album or artist UUID.
        :param item_name: Human-readable title displayed in the MA tasks panel.
        :param media_type: ``MediaType.ALBUM`` or ``MediaType.ARTIST``.
        """
        task_id = f"lidarr_request_{media_type.value}_{item_id}"

        async def _runner() -> None:
            await self._track_request_until_available(item_id, item_name, media_type)

        self.mass.tasks.run_background_task(
            name=f"Lidarr: {item_name}",
            handler=_runner,
            task_id=task_id,
            allow_cancel=True,
        )

    async def _track_request_until_available(
        self,
        item_id: str,
        item_name: str,
        media_type: MediaType,
        *,
        poll_interval: float = REQUEST_POLL_INTERVAL,
        max_wait: float = REQUEST_MAX_WAIT_SECONDS,
    ) -> None:
        """
        Poll Lidarr queue and library status, update MA task progress, then clean up and sync.

        :param item_id: MusicBrainz album or artist UUID.
        :param item_name: Display name of the requested album or artist.
        :param media_type: ``MediaType.ALBUM`` or ``MediaType.ARTIST``.
        :param poll_interval: Seconds between Lidarr status checks.
        :param max_wait: Maximum seconds to wait before timing out.
        """
        self.mass.tasks.update_current_task_progress(5, f"Searching in Lidarr: {item_name}")
        deadline = asyncio.get_running_loop().time() + max_wait

        while asyncio.get_running_loop().time() < deadline:
            if await self._is_item_available_in_lidarr(item_id, media_type):
                self.mass.tasks.update_current_task_progress(
                    90, "Imported in Lidarr — removing request"
                )
                await self._remove_request_from_library(item_id, media_type)
                self.mass.tasks.update_current_task_progress(
                    95, "Scanning Navidrome & syncing Music Assistant"
                )
                await self._trigger_local_rescan()
                self.mass.tasks.update_current_task_progress(100, "Available in library")
                return

            queue_progress = await self._find_queue_progress(item_id, media_type)
            if queue_progress is not None:
                pct, status = queue_progress
                # Map 0..100 download progress into 10..85 overall task progress.
                task_pct = 10 + round(pct * 0.75)
                self.mass.tasks.update_current_task_progress(task_pct, f"Lidarr: {status} ({pct}%)")
            else:
                self.mass.tasks.update_current_task_progress(
                    8, f"Waiting for download in Lidarr: {item_name}"
                )

            await asyncio.sleep(poll_interval)

        self.logger.warning(
            "Timed out waiting for Lidarr import of %s (%s) after %ss",
            item_name,
            item_id,
            max_wait,
        )

    async def _is_item_available_in_lidarr(self, item_id: str, media_type: MediaType) -> bool:
        """
        Return True once the requested album or artist has imported tracks in Lidarr.

        :param item_id: MusicBrainz album or artist UUID.
        :param media_type: ``MediaType.ALBUM`` or ``MediaType.ARTIST``.
        """
        available = False
        try:
            if media_type == MediaType.ALBUM:
                albums = await self.client.get_albums(foreign_album_id=item_id)
                stats = albums[0].get("statistics") if albums else None
                if isinstance(stats, dict):
                    track_files = int(stats.get("trackFileCount") or 0)
                    pct = float(stats.get("percentOfTracks") or 0)
                    available = track_files > 0 and pct >= 100.0
            elif media_type == MediaType.ARTIST:
                artist = await self.client.get_artist_by_foreign_id(item_id)
                stats = artist.get("statistics") if artist is not None else None
                if isinstance(stats, dict):
                    available = int(stats.get("trackFileCount") or 0) > 0
        except LidarrError as err:
            self.logger.debug("Lidarr availability check failed for %s: %s", item_id, err)
        return available

    async def _find_queue_progress(
        self, item_id: str, media_type: MediaType
    ) -> tuple[int, str] | None:
        """
        Return ``(percent, status)`` if the item is currently in the Lidarr download queue.

        :param item_id: MusicBrainz album or artist UUID.
        :param media_type: ``MediaType.ALBUM`` or ``MediaType.ARTIST``.
        """
        try:
            records = await self.client.get_queue()
        except LidarrError:
            return None

        for record in records:
            raw_album = record.get("album")
            raw_artist = record.get("artist")
            matched = (
                media_type == MediaType.ALBUM
                and isinstance(raw_album, dict)
                and raw_album.get("foreignAlbumId") == item_id
            ) or (
                media_type == MediaType.ARTIST
                and isinstance(raw_artist, dict)
                and raw_artist.get("foreignArtistId") == item_id
            )
            if not matched:
                continue
            status = str(record.get("status", "downloading")).lower()
            size = float(record.get("size") or 0)
            sizeleft = float(record.get("sizeleft") or 0)
            pct = max(0, min(100, round((1.0 - sizeleft / size) * 100))) if size > 0 else 0
            return pct, status
        return None

    async def _remove_request_from_library(self, item_id: str, media_type: MediaType) -> None:
        """
        Remove the Lidarr request placeholder from the Music Assistant library.

        If the item is already merged with another provider (e.g. Navidrome), only the Lidarr
        provider mapping is removed. Otherwise the placeholder library item itself is removed.

        :param item_id: MusicBrainz album or artist UUID.
        :param media_type: ``MediaType.ALBUM`` or ``MediaType.ARTIST``.
        """
        controller: Any
        if media_type == MediaType.ALBUM:
            controller = self.mass.music.albums
        elif media_type == MediaType.ARTIST:
            controller = self.mass.music.artists
        else:
            return

        lib_item = await controller.get_library_item_by_prov_id(item_id, self.instance_id)
        if lib_item is None:
            return

        other_mappings = [
            mapping
            for mapping in lib_item.provider_mappings
            if mapping.provider_instance != self.instance_id
        ]
        self._completing_ids.add(item_id)
        try:
            if other_mappings:
                await controller.remove_provider_mappings(lib_item.item_id, self.instance_id)
                self.logger.info("Removed Lidarr mapping from library item %r", lib_item.name)
            else:
                await controller.remove_item_from_library(lib_item.item_id, recursive=False)
                self.logger.info("Removed completed Lidarr request %r from library", lib_item.name)
        finally:
            self._completing_ids.discard(item_id)

    async def _cleanup_completed_requests(self) -> None:
        """Remove any Lidarr request placeholders in the MA library already imported in Lidarr."""
        for media_type, controller in (
            (MediaType.ALBUM, self.mass.music.albums),
            (MediaType.ARTIST, self.mass.music.artists),
        ):
            try:
                items = await controller.library_items(provider=self.instance_id, summary=False)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Skipping Lidarr library cleanup for %s: %s", media_type, err)
                continue
            for item in items:
                if not hasattr(item, "provider_mappings"):
                    continue
                for mapping in item.provider_mappings:
                    if mapping.provider_instance != self.instance_id:
                        continue
                    if await self._is_item_available_in_lidarr(mapping.item_id, media_type):
                        await self._remove_request_from_library(mapping.item_id, media_type)

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
                await self._cleanup_completed_requests()
                await self._trigger_local_rescan()

    async def _scan_subsonic_providers(self, target_ids: list[str]) -> None:
        """
        Trigger a library scan on any target OpenSubsonic/Navidrome provider and wait for it.

        :param target_ids: Provider instance IDs about to be synced in Music Assistant.
        """
        for instance_id in target_ids:
            prov = self.mass.get_provider(instance_id)
            conn = getattr(prov, "conn", None)
            if conn is None or not hasattr(conn, "start_scan"):
                continue
            try:
                self.logger.info(
                    "Triggering remote library scan on %s (Navidrome/OpenSubsonic)",
                    instance_id,
                )
                status = await conn.start_scan()
                if not hasattr(conn, "get_scan_status"):
                    continue
                deadline = asyncio.get_running_loop().time() + SUBSONIC_SCAN_MAX_WAIT_SECONDS
                while getattr(status, "scanning", False) and (
                    asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(SUBSONIC_SCAN_POLL_INTERVAL)
                    status = await conn.get_scan_status()
            except Exception as err:  # noqa: BLE001
                self.logger.warning(
                    "Remote Subsonic/Navidrome scan failed on %s: %s",
                    instance_id,
                    err,
                )

    async def _trigger_local_rescan(self) -> None:
        """Scan Navidrome/OpenSubsonic (if present) and trigger Music Assistant library sync."""
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
        await self._scan_subsonic_providers(targets)
        self.logger.info(
            "Lidarr imported new audio; triggering Music Assistant rescan for %s",
            targets,
        )
        await self.mass.music.start_sync(providers=targets)
