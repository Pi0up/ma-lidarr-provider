# Music Assistant — Lidarr Provider

Community music provider for **Music Assistant** that lets you search for missing **artists** and **albums** directly through your **Lidarr** server and trigger downloads from the Music Assistant UI.

## Features

- **Integrated Search (Artists & Albums)**: Queries the Lidarr v1 API (`/api/v1/search`) directly from the Music Assistant search bar. Album results are labeled with a customizable version tag (`Lidarr Request` by default) so Music Assistant can merge them cleanly with your local library.
- **Download on "Add to Library"**:
  - **Albums**: Adds the artist if missing (with other albums unmonitored), monitors only the requested album, and immediately triggers `AlbumSearch`. If the album already exists in Lidarr unmonitored, it enables monitoring and triggers a search.
  - **Artists**: Adds the artist using your configured monitoring mode (`future`, `all`, `missing`, `existing`, `none`, etc.) and triggers `ArtistSearch`.
  - **Safe Playback Guard**: Clicking *Play* on a Lidarr placeholder refuses direct playback with a clear message instead of triggering an unintended download.
- **Live Progress Tracking in Music Assistant**: Each request spawns a native Music Assistant background task (`Lidarr: <Name>`) that updates progress in real time from Lidarr's download queue until import completes.
- **Automatic Cleanup**: Once Lidarr finishes importing the requested album or artist, the temporary `Lidarr Request` placeholder (or its Lidarr mapping) is automatically removed from your Music Assistant library.
- **Forced Navidrome / OpenSubsonic + Music Assistant Sync**: When an import completes, the provider automatically triggers a remote library scan on your configured OpenSubsonic / Navidrome server (`start_scan` + `get_scan_status`), waits for it to finish, and then triggers a Music Assistant library rescan so the newly downloaded album appears immediately ready to play.
- **Browse View**: Exposes **Downloading (Queue)** (with download percentage) and **Missing / Requested Albums** under **Browse → Lidarr**.
- **Two-Step Setup Wizard**: Enter your Lidarr URL and API key in step 1, then pick your Root Folder, Quality Profile, Metadata Profile, artist monitoring mode, and optional tag from dropdowns loaded dynamically from Lidarr in step 2.

## Usage

1. Open **Search** in Music Assistant and make sure **Library only** is disabled.
2. Search for an artist or album not yet in your library. Results from Lidarr show the `Lidarr Request` label.
3. Open the album or artist and click **Add to library** (`+` or `⋮ → Add to library`).
4. Monitor progress directly in the **Tasks** panel at the top of Music Assistant or under **Browse → Lidarr**.
5. Once Lidarr imports the release, the placeholder disappears automatically and the real album is synced from Navidrome / your local library.

## Deployment with Docker + Home Assistant

Music Assistant loads local custom providers via `--dev-providers-dir` with `PYTHONDEVMODE=1`. The included `docker-compose.yml` runs Music Assistant Server with this provider mounted read-only and connects cleanly to Home Assistant through the official **Music Assistant** integration.

### 1. Prepare the Docker host

Clone this repository on your Docker / Proxmox host and create `.env` from `.env.example`:

```bash
git clone https://github.com/Pi0up/ma-lidarr-provider.git
cd ma-lidarr-provider
cp .env.example .env
```

Edit `.env` to adjust `MA_IMAGE_TAG`, `TZ`, `MA_DATA_DIR`, and optionally uncomment `MUSIC_DIR` in both `.env` and `docker-compose.yml` to mount your local music folder read-only at `/media`.

### 2. Start Music Assistant Server

```bash
docker compose up -d
docker compose logs -f music-assistant-server
```

### 3. Configure Lidarr in Music Assistant

1. Open `http://<SERVER_IP>:8095`.
2. Go to **Settings → Music Sources → Add Music Source → Lidarr**.
3. **Step 1**: Enter your Lidarr URL (e.g. `http://192.168.1.50:8686`) and API key (*Lidarr → Settings → General → Security*).
4. **Step 2**: Choose your Root Folder, Quality Profile, Metadata Profile, artist monitoring mode, and optional Lidarr tag.

### 4. Connect Home Assistant

1. Stop the old Music Assistant Home Assistant add-on if one is still running.
2. In Home Assistant, go to **Settings → Devices & services → Add integration → Music Assistant**.
3. Point it to `http://<SERVER_IP>:8095`.

### 5. Update the provider

```bash
git pull
docker compose restart music-assistant-server
```

## Local Development & Testing

```bash
# Install the development environment (Python 3.14 + music-assistant)
uv sync

# Start a local test server on http://localhost:8095
./scripts/run.sh

# Run unit and integration tests
uv run pytest

# Run linting, formatting, type checking and spell checking
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run codespell
```
