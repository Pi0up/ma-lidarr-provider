"""Constants for the Lidarr Music Assistant provider."""

from __future__ import annotations

API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 20
DEFAULT_VERSION_TAG = "Lidarr Request"
DEFAULT_POLL_INTERVAL = 60
DEFAULT_ARTIST_MONITOR = "future"

# Config entry keys
CONF_URL = "url"
CONF_API_KEY = "api_key"
CONF_ROOT_FOLDER = "root_folder"
CONF_QUALITY_PROFILE = "quality_profile"
CONF_METADATA_PROFILE = "metadata_profile"
CONF_ARTIST_MONITOR = "artist_monitor"
CONF_TAG_ID = "tag_id"
CONF_VERSION_TAG = "version_tag"
CONF_SYNC_PROVIDER = "sync_provider"
CONF_POLL_INTERVAL = "poll_interval"
CONF_ACTION_LOAD_PROFILES = "load_profiles"

# Special option values
SYNC_PROVIDER_AUTO = "auto"
SYNC_PROVIDER_NONE = "none"
TAG_NONE = "none"

# Lidarr artist monitor modes
ARTIST_MONITOR_MODES: tuple[tuple[str, str], ...] = (
    ("future", "Future albums only"),
    ("all", "All albums"),
    ("missing", "Missing albums"),
    ("existing", "Existing albums"),
    ("latest", "Latest album"),
    ("first", "First album"),
    ("none", "None (artist only)"),
)

# Browse folder paths
BROWSE_QUEUE = "queue"
BROWSE_MISSING = "missing"

# Lidarr history event type for imported track files
LIDARR_EVENT_TRACK_IMPORTED = 3
