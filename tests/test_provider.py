"""Integration tests for the Lidarr provider loaded in a real Music Assistant server."""

from __future__ import annotations

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

    # Adding the album to library requests only that album in Lidarr.
    added = await provider.library_add(album)
    assert added is True
    assert len(state.added_artists) == 1
    assert state.added_artists[0]["addOptions"]["albumsToMonitor"] == [ALBUM_MBID]


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
