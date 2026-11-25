"""Interactive two-step setup flow for the Lidarr Music Assistant provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .client import LidarrAuthError, LidarrClient, LidarrError
from .constants import (
    ARTIST_MONITOR_MODES,
    CONF_API_KEY,
    CONF_ARTIST_MONITOR,
    CONF_METADATA_PROFILE,
    CONF_QUALITY_PROFILE,
    CONF_ROOT_FOLDER,
    CONF_TAG_ID,
    CONF_URL,
    DEFAULT_ARTIST_MONITOR,
    TAG_NONE,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Drive the two-step setup wizard for a new Lidarr provider instance.

    Step 1 collects the Lidarr URL and API key and verifies connectivity.
    Step 2 presents dropdowns loaded dynamically from Lidarr for root folder, quality profile,
    metadata profile, artist monitoring mode, and optional tag.

    :param session: Setup flow session used to interact with the user.
    """
    setup_data: dict[str, ConfigValueType] = dict(session.context.setup_data)
    errors: dict[str, str | SetupFlowError] | None = None
    root_folders: list[dict[str, Any]] = []
    quality_profiles: list[dict[str, Any]] = []
    metadata_profiles: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []

    # Step 1: Collect URL & API key and load Lidarr profiles
    while True:
        connection_entries = [
            ConfigEntry(
                key=CONF_URL,
                type=ConfigEntryType.STRING,
                label="Lidarr URL",
                description="Base URL of your Lidarr server (e.g. http://192.168.1.50:8686).",
                required=True,
                value=setup_data.get(CONF_URL),
            ),
            ConfigEntry(
                key=CONF_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                label="API Key",
                description="Found in Lidarr under Settings > General > Security.",
                required=True,
                value=setup_data.get(CONF_API_KEY),
            ),
        ]
        submitted = await session.form(
            connection_entries,
            step_id="connection",
            errors=errors,
            last_step=False,
        )
        setup_data.update(submitted)
        url = str(setup_data.get(CONF_URL) or "").strip()
        api_key = str(setup_data.get(CONF_API_KEY) or "").strip()

        client = LidarrClient(session.mass.http_session, url, api_key)
        try:
            await client.get_system_status()
            root_folders, quality_profiles, metadata_profiles, tags = await asyncio.gather(
                client.get_root_folders(),
                client.get_quality_profiles(),
                client.get_metadata_profiles(),
                client.get_tags(),
            )
            break
        except LidarrAuthError:
            errors = {CONF_API_KEY: "Invalid Lidarr API key"}
        except LidarrError as err:
            errors = {"base": str(err)}

    root_folder_options = [
        ConfigValueOption(str(rf["path"]), str(rf["path"])) for rf in root_folders if rf.get("path")
    ]
    quality_options = [
        ConfigValueOption(str(qp["name"]), str(qp["id"]))
        for qp in quality_profiles
        if qp.get("id") is not None
    ]
    metadata_options = [
        ConfigValueOption(str(mp["name"]), str(mp["id"]))
        for mp in metadata_profiles
        if mp.get("id") is not None
    ]
    tag_options = [
        ConfigValueOption("None", TAG_NONE),
        *(
            ConfigValueOption(str(tag["label"]), str(tag["id"]))
            for tag in tags
            if tag.get("id") is not None
        ),
    ]
    monitor_options = [ConfigValueOption(label, value) for value, label in ARTIST_MONITOR_MODES]

    # Step 2: Select Lidarr profiles and finish setup
    errors = None
    while True:
        profile_entries = [
            ConfigEntry(
                key=CONF_ROOT_FOLDER,
                type=ConfigEntryType.STRING,
                label="Root Folder",
                description="Lidarr root folder where requested music will be stored.",
                options=root_folder_options,
                default_value=root_folder_options[0].value if root_folder_options else "",
                value=setup_data.get(CONF_ROOT_FOLDER),
                required=bool(root_folder_options),
            ),
            ConfigEntry(
                key=CONF_QUALITY_PROFILE,
                type=ConfigEntryType.STRING,
                label="Quality Profile",
                description="Lidarr quality profile used for newly added artists.",
                options=quality_options,
                default_value=quality_options[0].value if quality_options else "",
                value=setup_data.get(CONF_QUALITY_PROFILE),
                required=bool(quality_options),
            ),
            ConfigEntry(
                key=CONF_METADATA_PROFILE,
                type=ConfigEntryType.STRING,
                label="Metadata Profile",
                description="Lidarr metadata profile used for newly added artists.",
                options=metadata_options,
                default_value=metadata_options[0].value if metadata_options else "",
                value=setup_data.get(CONF_METADATA_PROFILE),
                required=bool(metadata_options),
            ),
            ConfigEntry(
                key=CONF_ARTIST_MONITOR,
                type=ConfigEntryType.STRING,
                label="Artist Monitoring Mode",
                description="Which albums Lidarr monitors when you add a full artist.",
                options=monitor_options,
                default_value=DEFAULT_ARTIST_MONITOR,
                value=setup_data.get(CONF_ARTIST_MONITOR, DEFAULT_ARTIST_MONITOR),
                required=True,
            ),
            ConfigEntry(
                key=CONF_TAG_ID,
                type=ConfigEntryType.STRING,
                label="Lidarr Tag",
                description="Optional Lidarr tag applied to artists requested via Music Assistant.",
                options=tag_options,
                default_value=TAG_NONE,
                value=setup_data.get(CONF_TAG_ID, TAG_NONE),
                required=False,
            ),
        ]
        submitted = await session.form(
            profile_entries,
            step_id="profiles",
            errors=errors,
            last_step=True,
        )
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
        except SetupFlowError as err:
            errors = {"base": err}
        else:
            return
