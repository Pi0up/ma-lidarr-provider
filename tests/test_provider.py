"""Integration tests for the Lidarr provider loaded in a real Music Assistant server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderTier
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, BrowseFolder

from lidarr import get_config_entries
from lidarr.constants import (
    BROWSE_MISSING,
    BROWSE_QUEUE,
    CONF_ACTION_LOAD_PROFILES,
    CONF_API_KEY,
    CONF_ARTIST_MONITOR,
    CONF_METADATA_PROFILE,
    CONF_QUALITY_PROFILE,
    CONF_ROOT_FOLDER,
    CONF_TAG_ID,
    CONF_URL,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests import DOMAIN
from tests.test_client import (
    ALBUM_MBID,
    API_KEY,
    ARTIST_MBID,
    FakeLidarrState,
    build_fake_lidarr_app,
)

if TYPE_CHECKING:
    from aiohttp.test_utils import TestServer


async def _setup_lidarr_provider(
    mass: MusicAssistant,
    server: TestServer,
) -> MusicProvider:
    """Run the two-step Lidarr setup flow against the fake Lidarr server."""
    step1 = await mass.config.setup_provider(DOMAIN)
    assert step1.step_id == "connection"

    step2 = await mass.config.submit_setup_flow(
        step1.flow_id,
        {
            CONF_URL: str(server.make_url("/")),
            CONF_API_KEY: API_KEY,
        },
    )
    assert step2.step_id == "profiles"
    assert step2.entries is not None
    by_key = {entry.key: entry for entry in step2.entries}
    assert [opt.value for opt in by_key[CONF_ROOT_FOLDER].options] == ["/music"]

    finish = await mass.config.submit_setup_flow(
        step2.flow_id,
        {
            CONF_ROOT_FOLDER: "/music",
            CONF_QUALITY_PROFILE: "1",
            CONF_METADATA_PROFILE: "1",
            CONF_ARTIST_MONITOR: "future",
            CONF_TAG_ID: "7",
        },
    )
    assert finish.result is not None
    provider = mass.get_provider(finish.result["instance_id"])
    assert isinstance(provider, MusicProvider)
    return provider


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the Lidarr provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_config_entries_load_profiles(mass: MusicAssistant, aiohttp_server: Any) -> None:
    """Dynamic config entries populate root folders, profiles and tags from Lidarr."""
    state = FakeLidarrState()
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))

    entries = await get_config_entries(
        mass,
        action=CONF_ACTION_LOAD_PROFILES,
        values={
            CONF_URL: str(server.make_url("/")),
            CONF_API_KEY: API_KEY,
        },
    )
    by_key = {entry.key: entry for entry in entries}
    assert [opt.value for opt in by_key[CONF_ROOT_FOLDER].options] == ["/music"]


async def test_search_and_library_add(mass: MusicAssistant, aiohttp_server: Any) -> None:
    """Searching returns Lidarr artists and albums, and library_add triggers Lidarr downloads."""
    state = FakeLidarrState()
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    provider = await _setup_lidarr_provider(mass, server)

    result = await provider.search("Daft Punk", [MediaType.ARTIST, MediaType.ALBUM], limit=5)
    assert len(result.artists) == 1
    assert len(result.albums) == 1

    artist = result.artists[0]
    album = result.albums[0]
    assert isinstance(artist, Artist)
    assert isinstance(album, Album)
    assert artist.name == "Daft Punk"
    assert (ExternalID.MB_ARTIST, ARTIST_MBID) in artist.external_ids
    assert album.name == "Discovery"
    assert album.version == "Lidarr Request"
    assert album.year == 2001
    assert (ExternalID.MB_RELEASEGROUP, ALBUM_MBID) in album.external_ids

    # Direct playback must fail explicitly without triggering a download.
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details(album.item_id, MediaType.ALBUM)
    assert state.added_artists == []
    assert state.commands == []

    # Adding the album to library adds the artist, monitors only that album and triggers search.
    added = await provider.library_add(album)
    assert added is True
    assert len(state.added_artists) == 1
    assert state.monitored_calls == [{"albumIds": [110], "monitored": True}]
    assert state.commands == [{"name": "AlbumSearch", "albumIds": [110]}]

    # Opening the album in MA must not fail when fetching album tracks.
    assert await provider.get_album_tracks(album.item_id) == []

    # Removing the album from library cancels the Lidarr request.
    removed = await provider.library_remove(album.item_id, MediaType.ALBUM)
    assert removed is True
    assert state.monitored_calls[-1] == {"albumIds": [110], "monitored": False}


async def test_browse_queue_and_missing(mass: MusicAssistant, aiohttp_server: Any) -> None:
    """Browsing exposes Lidarr active downloads and monitored missing albums."""
    state = FakeLidarrState()
    state.queue_records.append(
        {
            "status": "downloading",
            "size": 100.0,
            "sizeleft": 25.0,
            "artist": {"foreignArtistId": ARTIST_MBID, "artistName": "Daft Punk"},
            "album": {
                "foreignAlbumId": ALBUM_MBID,
                "title": "Discovery",
                "albumType": "Album",
                "releaseDate": "2001-03-12T00:00:00Z",
            },
        }
    )
    state.missing_records.append(
        {
            "foreignAlbumId": ALBUM_MBID,
            "title": "Discovery",
            "albumType": "Album",
            "releaseDate": "2001-03-12T00:00:00Z",
            "artist": {"foreignArtistId": ARTIST_MBID, "artistName": "Daft Punk"},
        }
    )
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    provider = await _setup_lidarr_provider(mass, server)

    root = await provider.browse(f"{provider.instance_id}://")
    assert len(root) == 2
    assert all(isinstance(item, BrowseFolder) for item in root)

    queue_items = await provider.browse(f"{provider.instance_id}://{BROWSE_QUEUE}")
    assert len(queue_items) == 1
    assert isinstance(queue_items[0], Album)
    assert queue_items[0].version == "Lidarr: downloading (75%)"

    missing_items = await provider.browse(f"{provider.instance_id}://{BROWSE_MISSING}")
    assert len(missing_items) == 1
    assert isinstance(missing_items[0], Album)
    assert missing_items[0].version == "Lidarr: missing"


async def test_trigger_local_rescan_calls_start_sync(
    mass: MusicAssistant,
    aiohttp_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Lidarr imports an album, _trigger_local_rescan invokes mass.music.start_sync."""
    state = FakeLidarrState()
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    provider = await _setup_lidarr_provider(mass, server)

    mock_start_sync = AsyncMock()
    monkeypatch.setattr(mass.music, "start_sync", mock_start_sync)
    lidarr_prov: Any = provider
    lidarr_prov._sync_provider = "filesystem_local"  # noqa: SLF001

    await lidarr_prov._trigger_local_rescan()  # noqa: SLF001
    mock_start_sync.assert_awaited_once_with(providers=["filesystem_local"])


async def test_request_tracking_progress_cleanup_and_navidrome_sync(
    mass: MusicAssistant,
    aiohttp_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracking a request reports progress, removes the MA request, scans Navidrome and syncs MA."""
    state = FakeLidarrState()
    state.queue_records.append(
        {
            "status": "downloading",
            "size": 100.0,
            "sizeleft": 50.0,
            "artist": {"foreignArtistId": ARTIST_MBID, "artistName": "Daft Punk"},
            "album": {
                "foreignAlbumId": ALBUM_MBID,
                "title": "Discovery",
                "albumType": "Album",
                "releaseDate": "2001-03-12T00:00:00Z",
            },
        }
    )
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    provider = await _setup_lidarr_provider(mass, server)
    lidarr_prov: Any = provider

    progress_updates: list[tuple[int | None, str | None]] = []

    def record_progress(progress: int | None, text: str | None = None) -> None:
        progress_updates.append((progress, text))
        # On the first queue progress tick, mark the album as imported in Lidarr.
        if progress == 48:
            state.queue_records.clear()
            state.albums.append(
                {
                    "id": 42,
                    "artistId": 10,
                    "foreignAlbumId": ALBUM_MBID,
                    "title": "Discovery",
                    "monitored": True,
                    "statistics": {"trackFileCount": 14, "percentOfTracks": 100.0},
                }
            )

    monkeypatch.setattr(mass.tasks, "update_current_task_progress", record_progress)

    # Simulate a placeholder Lidarr album present in the MA library.
    fake_lib_item = SimpleNamespace(
        item_id="99",
        name="Discovery",
        provider_mappings=[SimpleNamespace(provider_instance=provider.instance_id)],
    )
    monkeypatch.setattr(
        mass.music.albums,
        "get_library_item_by_prov_id",
        AsyncMock(return_value=fake_lib_item),
    )
    mock_remove = AsyncMock()
    monkeypatch.setattr(mass.music.albums, "remove_item_from_library", mock_remove)

    # Simulate an OpenSubsonic/Navidrome provider targeted for rescan.
    fake_conn = SimpleNamespace(
        start_scan=AsyncMock(return_value=SimpleNamespace(scanning=True)),
        get_scan_status=AsyncMock(return_value=SimpleNamespace(scanning=False)),
    )
    fake_navidrome = SimpleNamespace(
        instance_id="opensubsonic--navidrome",
        domain="opensubsonic",
        is_streaming_provider=False,
        conn=fake_conn,
    )
    orig_get_provider = mass.get_provider
    monkeypatch.setattr(
        mass,
        "get_provider",
        lambda pid: fake_navidrome if pid == "opensubsonic--navidrome" else orig_get_provider(pid),
    )
    mock_start_sync = AsyncMock()
    monkeypatch.setattr(mass.music, "start_sync", mock_start_sync)
    lidarr_prov._sync_provider = "opensubsonic--navidrome"  # noqa: SLF001

    await lidarr_prov._track_request_until_available(  # noqa: SLF001
        ALBUM_MBID,
        "Discovery",
        MediaType.ALBUM,
        poll_interval=0.01,
        max_wait=5.0,
    )

    assert any(pct == 48 and text and "50%" in text for pct, text in progress_updates)
    assert progress_updates[-1] == (100, "Available in library")
    mock_remove.assert_awaited_once_with("99", recursive=False)
    fake_conn.start_scan.assert_awaited_once()
    fake_conn.get_scan_status.assert_awaited_once()
    mock_start_sync.assert_awaited_once_with(providers=["opensubsonic--navidrome"])
