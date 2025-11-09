# Storage Manager

Storage Manager is a Django 5.2 application for uploading, organizing, and streaming user-generated assets on top of Google Cloud Storage (GCS). Each upload is persisted in SQLite (by default), copied to GCS, optionally segmented into HLS playlists with ffmpeg, and then surfaced through a Bootstrap UI with dashboards for active, deleted, and video-only assets.

## Highlights
- Google Cloud Storage integration with signed download and streaming URLs (`storage/storage_util.py`).
- Rich dashboard with sorting, rename, soft-delete, restore, and purge flows (`storage/views.py` and `templates/*.html`).
- Background processing pipeline driven by Celery + Redis to offload uploads and HLS segmentation (`storage/upload_helpers.py`).
- Custom email-based user model (`storage/models.py`) with Django auth views for login/logout.
- Docker-first workflow with optional local development via `uv`/virtualenv.

## Stack and components
- **Django app (`storage/`)** – URLs, views, custom models, templates, and tests.
- **Celery worker + Redis** – `schedule_primary_upload`, `_schedule_video_processing`, and cleanup tasks handle long-running work.
- **ffmpeg** – Segments video uploads into `/segmented` manifests so `/files/<id>/play/` can stream with signed URLs.
- **Google Cloud Storage** – All persisted assets and derived segments live in your bucket; service account credentials are mounted at runtime.

## Quick start (Docker Compose)
1. Copy `.env` (see the sample in the repo root) and fill in the values described below. At minimum you need `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `GOOGLE_APPLICATION_CREDENTIALS`, and `GCS_BUCKET_NAME`.
2. Place your service-account JSON at `secrets/gcs-key.json`. Docker mounts it inside the containers at `/var/secrets/gcs-key.json`.
3. Start the stack:
   ```shell
   docker compose up --build
   ```
   This launches:
   - `web`: Django app + runserver + automatic migrations.
   - `celery`: worker processing uploads/ffmpeg tasks.
   - `redis`: broker/result backend for Celery.
4. Create an initial admin/superuser (email is the username):
   ```shell
   docker compose run --rm \
     -e DJANGO_SUPERUSER_USERNAME=admin@example.com \
     -e DJANGO_SUPERUSER_EMAIL=admin@example.com \
     -e DJANGO_SUPERUSER_PASSWORD=changeme \
     web python manage.py createsuperuser --noinput
   ```
5. Visit `http://localhost:8000` and log in via `/accounts/login/`.

## Local development without Docker
Requirements:
- Python 3.11+
- Redis 7+ reachable at `redis://localhost:6379/0` (or update `CELERY_BROKER_URL`)
- ffmpeg on your `PATH`
- Google service account credentials with access to the bucket
- Optional: [`uv`](https://github.com/astral-sh/uv) for dependency management (matches the Docker image)

Steps:
1. Create `.env` (you can reuse the Docker values) and ensure `GOOGLE_APPLICATION_CREDENTIALS` points to the JSON on your machine.
2. Install dependencies:
   ```shell
   uv sync            # or: python -m venv .venv && source .venv/Scripts/activate && pip install -r requirements
   ```
3. Apply migrations and create a user:
   ```shell
   uv run python manage.py migrate
   uv run python manage.py createsuperuser
   ```
4. Run Django:
   ```shell
   uv run python manage.py runserver 0.0.0.0:8000
   ```
5. In another terminal, start the Celery worker so uploads and ffmpeg jobs are processed:
   ```shell
   uv run celery -A storage worker -l info
   ```
6. Ensure Redis and ffmpeg stay running; without them uploads will stay in `UPLOADING/PROCESSING`.

## Environment reference

| Variable | Default (settings.py/.env) | Purpose |
| --- | --- | --- |
| `DJANGO_DEBUG` | `true` | Enable Django debug tooling; set `false` in production. |
| `DJANGO_SECRET_KEY` | `insecure-dev-key-change-me` | Required for sessions/CSRF – override in production. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | Comma-separated hostnames the app will serve. |
| `DJANGO_TRUSTED_ORIGINS` | _empty_ | Optional CSRF trusted origins (`http://host:port`). |
| `DJANGO_MEDIA_ROOT` | `<repo>/media` | Where temporary uploads and HLS workdirs live. |
| `DJANGO_TIME_ZONE` | `UTC` | Controls Django/Celery timezone. |
| `DJANGO_LOG_LEVEL` / `STORAGE_LOG_LEVEL` | `INFO` | Logging thresholds for Django core vs. app loggers. |
| `GOOGLE_APPLICATION_CREDENTIALS` | _required_ | Path to the service-account JSON inside the container/host. |
| `GCS_BUCKET_NAME` | _required_ | Target bucket for uploads. |
| `GCP_PROJECT` | _optional_ | Overrides auto-detected Project ID for the client. |
| `GCS_CREDENTIALS_FILE` | _optional_ | Alternate path to credentials (used by `storage_util`). |
| `GCS_UPLOAD_PREFIX` | _optional_ | Prepended object prefix (e.g., `env/prod`). |
| `GCS_PUBLIC_BASE_URL` | _optional_ | CDN/base URL for building public object links. |
| `GCS_SIGNED_URL_TTL` | `3600` | Lifetime (seconds) for signed download/segment URLs. |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Where Celery publishes jobs. For local dev, point to your Redis instance. |
| `CELERY_RESULT_BACKEND` | _same as broker_ | Celery result backend; reuse Redis or point elsewhere. |
| `VIDEO_PROCESSING_ENABLED` | `true` | Toggle ffmpeg-based HLS processing. Set `false` if ffmpeg is unavailable. |
| `DJANGO_SUPERUSER_*` | _n/a_ | Only needed when creating a superuser via non-interactive commands. |

Any other `GCS_*` env documented in `storage/storage_util.py` behave identically whether set as Django settings or environment variables.

## Application walk-through
- `/` or `/dashboard/` – Authenticated dashboard listing every active file with sorting controls, rename, delete, and download buttons.
- `/upload/` – Basic file uploader that streams to disk, records metadata (`StoredFile`), then enqueues `schedule_primary_upload`.
- `/videos/` – Filters to video uploads (`content_type` starts with `video/`). Displays processing status and exposes playback when the manifest is ready.
- `/bin/` – Soft-deleted files with restore and permanent purge actions (removes rows plus their prefixes from GCS).
- `/files/<id>/download/` – Generates a V4 signed URL for the backing object and redirects (404s if the file is deleted or owned by another user).
- `/files/<id>/play/` – Fetches the segmented manifest, rewrites segment URLs with signed links, and returns an HLS playlist download for the requester.
- `/video/` – Simple manifest viewer useful for debugging bucket contents.
- `/accounts/login/` / `/accounts/logout/` – Default Django auth views bound to the custom email-based user model (`storage.models.User`).

### Storage & processing pipeline
1. `upload_file` writes the upload to `MEDIA_ROOT/temp/uploads/<uuid>/`.
2. Metadata is stored in `StoredFile` with an initial status of `UPLOADING`.
3. `schedule_primary_upload` runs in Celery to push the file to GCS and advance statuses.
4. If the file is a video and `VIDEO_PROCESSING_ENABLED=true`, `_schedule_video_processing`:
   - Runs ffmpeg with HLS flags to produce `output.m3u8` + `segment_*.ts`.
   - Uploads each derived file under `<user>/<uuid>/segmented/`.
5. `_schedule_local_cleanup` wipes the temp workdir once the job reaches `READY`.

The dashboard and APIs display human-friendly categories (`video`, `photo`, `file`) using helper logic in `storage/views.py`.

## Running tests
All Django tests live under `storage/tests/` and can be exercised via:
```shell
uv run python manage.py test -v 2
# or inside Docker:
docker compose run --rm web python manage.py test -v 2
```
Tests use SQLite and override settings so no external services are required.

## Tips & troubleshooting
- **ffmpeg missing?** Set `VIDEO_PROCESSING_ENABLED=false` to skip segmentation until it is installed.
- **Redis not running?** Uploads will never leave `UPLOADING`. Start Redis locally or update `CELERY_BROKER_URL` to a reachable instance.
- **Different storage provider?** `storage/storage_util.py` is the single integration point with GCS. Replace it or add alternate clients if needed.
- **Large files / slow networks?** The dashboard surfaces status transitions; check logs (set `STORAGE_LOG_LEVEL=DEBUG`) for detailed timing.

With these pieces in place you can log in, upload files, stream video playlists, and manage your bucket contents entirely through the web UI.
