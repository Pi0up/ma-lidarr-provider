"""Unit tests for the async Lidarr v1 REST API client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aiohttp import web

from lidarr.client import (
    LidarrAuthError,
    LidarrClient,
    LidarrNotFoundError,
    LidarrRequestOptions,
)

if TYPE_CHECKING:
    from aiohttp.test_utils import TestServer

API_KEY = "test-secret-key"
ARTIST_MBID = "11111111-1111-1111-1111-111111111111"
ALBUM_MBID = "22222222-2222-2222-2222-222222222222"


class FakeLidarrState:
    """In-memory state backing the fake Lidarr HTTP server."""

    def __init__(self) -> None:
        """Initialize empty fake Lidarr state."""
        self.artists: list[dict[str, Any]] = []
        self.albums: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.monitored_calls: list[dict[str, Any]] = []
        self.added_artists: list[dict[str, Any]] = []
        self.updated_artists: list[dict[str, Any]] = []
        self.queue_records: list[dict[str, Any]] = []
        self.deleted_queue_ids: list[int] = []
        self.missing_records: list[dict[str, Any]] = []
        self.history_records: list[dict[str, Any]] = []


class _FakeLidarrHandlers:
    """HTTP route handlers for the fake Lidarr v1 server."""

    def __init__(self, state: FakeLidarrState) -> None:
        """Store mutable server state."""
        self.state = state

    async def system_status(self, _request: web.Request) -> web.Response:
        return web.json_response({"version": "2.4.0.0"})

    async def root_folders(self, _request: web.Request) -> web.Response:
        return web.json_response([{"id": 1, "path": "/music"}])

    async def quality_profiles(self, _request: web.Request) -> web.Response:
        return web.json_response([{"id": 1, "name": "Lossless"}])

    async def metadata_profiles(self, _request: web.Request) -> web.Response:
        return web.json_response([{"id": 1, "name": "Standard"}])

    async def tags(self, _request: web.Request) -> web.Response:
        return web.json_response([{"id": 7, "label": "ma-request"}])

    async def search(self, request: web.Request) -> web.Response:
        term = request.query.get("term", "").lower()
        if "daft" not in term:
            return web.json_response([])
        return web.json_response(
            [
                {
                    "artist": {
                        "foreignArtistId": ARTIST_MBID,
                        "artistName": "Daft Punk",
                        "overview": "French electronic duo",
                        "images": [
                            {
                                "coverType": "poster",
                                "remoteUrl": "https://example.invalid/daft.jpg",
                            }
                        ],
                    }
                },
                {
                    "album": {
                        "foreignAlbumId": ALBUM_MBID,
                        "title": "Discovery",
                        "albumType": "Album",
                        "releaseDate": "2001-03-12T00:00:00Z",
                        "artist": {
                            "foreignArtistId": ARTIST_MBID,
                            "artistName": "Daft Punk",
                        },
                        "images": [
                            {
                                "coverType": "cover",
                                "remoteUrl": "https://example.invalid/discovery.jpg",
                            }
                        ],
                    }
                },
            ]
        )

    async def artist_lookup(self, request: web.Request) -> web.Response:
        term = request.query.get("term", "")
        if ARTIST_MBID in term:
            return web.json_response(
                [{"foreignArtistId": ARTIST_MBID, "artistName": "Daft Punk", "images": []}]
            )
        return web.json_response([])

    async def album_lookup(self, request: web.Request) -> web.Response:
        term = request.query.get("term", "")
        if ALBUM_MBID in term:
            return web.json_response(
                [
                    {
                        "foreignAlbumId": ALBUM_MBID,
                        "title": "Discovery",
                        "albumType": "Album",
                        "releaseDate": "2001-03-12T00:00:00Z",
                        "artist": {
                            "foreignArtistId": ARTIST_MBID,
                            "artistName": "Daft Punk",
                        },
                    }
                ]
            )
        return web.json_response([])

    async def get_artists(self, request: web.Request) -> web.Response:
        mb_id = request.query.get("mbId")
        if mb_id:
            matches = [a for a in self.state.artists if a.get("foreignArtistId") == mb_id]
            return web.json_response(matches)
        return web.json_response(self.state.artists)

    async def get_artist_by_id(self, request: web.Request) -> web.Response:
        artist_id = int(request.match_info["id"])
        for artist in self.state.artists:
            if artist.get("id") == artist_id:
                return web.json_response(artist)
        return web.json_response({"message": "Not found"}, status=404)

    async def add_artist(self, request: web.Request) -> web.Response:
        payload = await request.json()
        artist_id = len(self.state.artists) + 10
        payload["id"] = artist_id
        self.state.artists.append(payload)
        self.state.added_artists.append(payload)
        # Mimic Lidarr's post-add refresh populating the artist's albums unmonitored.
        if payload.get("foreignArtistId") == ARTIST_MBID and not any(
            a.get("foreignAlbumId") == ALBUM_MBID for a in self.state.albums
        ):
            self.state.albums.append(
                {
                    "id": 100 + artist_id,
                    "artistId": artist_id,
                    "foreignAlbumId": ALBUM_MBID,
                    "title": "Discovery",
                    "monitored": False,
                }
            )
        return web.json_response(payload, status=201)

    async def update_artist(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.state.updated_artists.append(payload)
        for idx, existing in enumerate(self.state.artists):
            if existing.get("id") == payload.get("id"):
                self.state.artists[idx] = payload
        return web.json_response(payload)

    async def get_albums(self, request: web.Request) -> web.Response:
        foreign_id = request.query.get("foreignAlbumId")
        artist_id = request.query.get("artistId")
        results = list(self.state.albums)
        if foreign_id:
            results = [a for a in results if a.get("foreignAlbumId") == foreign_id]
        if artist_id:
            results = [a for a in results if str(a.get("artistId")) == artist_id]
        return web.json_response(results)

    async def monitor_albums(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.state.monitored_calls.append(payload)
        for album in self.state.albums:
            if album.get("id") in payload.get("albumIds", []):
                album["monitored"] = payload.get("monitored", True)
        return web.json_response(self.state.albums)

    async def command(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.state.commands.append(payload)
        return web.json_response({"id": len(self.state.commands), **payload}, status=201)

    async def queue(self, _request: web.Request) -> web.Response:
        return web.json_response({"records": self.state.queue_records})

    async def delete_queue(self, request: web.Request) -> web.Response:
        queue_id = int(request.match_info["id"])
        self.state.deleted_queue_ids.append(queue_id)
        self.state.queue_records = [r for r in self.state.queue_records if r.get("id") != queue_id]
        return web.Response(status=204)

    async def wanted_missing(self, _request: web.Request) -> web.Response:
        return web.json_response({"records": self.state.missing_records})

    async def history(self, _request: web.Request) -> web.Response:
        return web.json_response({"records": self.state.history_records})


def build_fake_lidarr_app(state: FakeLidarrState) -> web.Application:
    """
    Create an aiohttp web application mimicking the Lidarr v1 API.

    :param state: Mutable state inspected and mutated by test handlers.
    """

    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if request.headers.get("X-Api-Key") != API_KEY:
            return web.json_response({"message": "Unauthorized"}, status=401)
        resp: web.StreamResponse = await handler(request)
        return resp

    handlers = _FakeLidarrHandlers(state)
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/api/v1/system/status", handlers.system_status)
    app.router.add_get("/api/v1/rootfolder", handlers.root_folders)
    app.router.add_get("/api/v1/qualityprofile", handlers.quality_profiles)
    app.router.add_get("/api/v1/metadataprofile", handlers.metadata_profiles)
    app.router.add_get("/api/v1/tag", handlers.tags)
    app.router.add_get("/api/v1/search", handlers.search)
    app.router.add_get("/api/v1/artist/lookup", handlers.artist_lookup)
    app.router.add_get("/api/v1/album/lookup", handlers.album_lookup)
    app.router.add_get("/api/v1/artist", handlers.get_artists)
    app.router.add_get("/api/v1/artist/{id}", handlers.get_artist_by_id)
    app.router.add_post("/api/v1/artist", handlers.add_artist)
    app.router.add_put("/api/v1/artist/{id}", handlers.update_artist)
    app.router.add_get("/api/v1/album", handlers.get_albums)
    app.router.add_put("/api/v1/album/monitor", handlers.monitor_albums)
    app.router.add_post("/api/v1/command", handlers.command)
    app.router.add_get("/api/v1/queue", handlers.queue)
    app.router.add_delete("/api/v1/queue/{id}", handlers.delete_queue)
    app.router.add_get("/api/v1/wanted/missing", handlers.wanted_missing)
    app.router.add_get("/api/v1/history", handlers.history)
    return app


async def test_auth_error(aiohttp_server: Any) -> None:
    """Client raises LidarrAuthError when the API key is rejected."""
    state = FakeLidarrState()
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    async with aiohttp.ClientSession() as session:
        client = LidarrClient(session, str(server.make_url("/")), "wrong-key", rate_limit_delay=0)
        with pytest.raises(LidarrAuthError):
            await client.get_system_status()


async def test_request_album_adds_new_artist_with_single_album(aiohttp_server: Any) -> None:
    """Requesting a missing album adds its artist with only that album monitored."""
    state = FakeLidarrState()
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    options = LidarrRequestOptions(
        root_folder_path="/music",
        quality_profile_id=1,
        metadata_profile_id=1,
        tag_ids=[7],
    )
    async with aiohttp.ClientSession() as session:
        client = LidarrClient(session, str(server.make_url("/")), API_KEY, rate_limit_delay=0)
        await client.request_album(ALBUM_MBID, options)

    assert len(state.added_artists) == 1
    added = state.added_artists[0]
    assert added["foreignArtistId"] == ARTIST_MBID
    assert added["rootFolderPath"] == "/music"
    assert added["tags"] == [7]
    assert added["addOptions"] == {
        "monitor": "none",
        "albumsToMonitor": [ALBUM_MBID],
        "searchForMissingAlbums": False,
    }
    # After artist refresh populates the album, the client explicitly monitors it and searches.
    assert state.monitored_calls == [{"albumIds": [110], "monitored": True}]
    assert state.commands == [{"name": "AlbumSearch", "albumIds": [110]}]


async def test_request_album_existing_unmonitored_triggers_search(aiohttp_server: Any) -> None:
    """Requesting an already-known unmonitored album enables monitoring and triggers search."""
    state = FakeLidarrState()
    state.albums.append(
        {
            "id": 42,
            "artistId": 10,
            "foreignAlbumId": ALBUM_MBID,
            "title": "Discovery",
            "monitored": False,
        }
    )
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    options = LidarrRequestOptions(
        root_folder_path="/music",
        quality_profile_id=1,
        metadata_profile_id=1,
    )
    async with aiohttp.ClientSession() as session:
        client = LidarrClient(session, str(server.make_url("/")), API_KEY, rate_limit_delay=0)
        await client.request_album(ALBUM_MBID, options)

    assert state.monitored_calls == [{"albumIds": [42], "monitored": True}]
    assert state.commands == [{"name": "AlbumSearch", "albumIds": [42]}]


async def test_request_artist_existing_unmonitored_updates_and_searches(
    aiohttp_server: Any,
) -> None:
    """Requesting an existing unmonitored artist monitors it and triggers ArtistSearch."""
    state = FakeLidarrState()
    state.artists.append(
        {
            "id": 10,
            "foreignArtistId": ARTIST_MBID,
            "artistName": "Daft Punk",
            "monitored": False,
        }
    )
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    options = LidarrRequestOptions(
        root_folder_path="/music",
        quality_profile_id=1,
        metadata_profile_id=1,
        artist_monitor="all",
    )
    async with aiohttp.ClientSession() as session:
        client = LidarrClient(session, str(server.make_url("/")), API_KEY, rate_limit_delay=0)
        await client.request_artist(ARTIST_MBID, options)

    assert len(state.updated_artists) == 1
    assert state.updated_artists[0]["monitored"] is True
    assert state.commands == [{"name": "ArtistSearch", "artistId": 10}]


async def test_request_unknown_album_raises_not_found(aiohttp_server: Any) -> None:
    """Requesting an unknown album MBID raises LidarrNotFoundError."""
    state = FakeLidarrState()
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    options = LidarrRequestOptions(
        root_folder_path="/music",
        quality_profile_id=1,
        metadata_profile_id=1,
    )
    async with aiohttp.ClientSession() as session:
        client = LidarrClient(session, str(server.make_url("/")), API_KEY, rate_limit_delay=0)
        with pytest.raises(LidarrNotFoundError):
            await client.request_album("99999999-9999-9999-9999-999999999999", options)


async def test_cancel_album_request_unmonitors_and_removes_queue(aiohttp_server: Any) -> None:
    """Cancelling an album request unmonitors it and deletes its active queue entry."""
    state = FakeLidarrState()
    state.albums.append(
        {
            "id": 42,
            "artistId": 10,
            "foreignAlbumId": ALBUM_MBID,
            "title": "Discovery",
            "monitored": True,
        }
    )
    state.queue_records.append(
        {
            "id": 77,
            "status": "downloading",
            "album": {"foreignAlbumId": ALBUM_MBID, "title": "Discovery"},
        }
    )
    server: TestServer = await aiohttp_server(build_fake_lidarr_app(state))
    async with aiohttp.ClientSession() as session:
        client = LidarrClient(session, str(server.make_url("/")), API_KEY, rate_limit_delay=0)
        cancelled = await client.cancel_album_request(ALBUM_MBID)

    assert cancelled is True
    assert state.monitored_calls == [{"albumIds": [42], "monitored": False}]
    assert state.deleted_queue_ids == [77]
