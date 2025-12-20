"""Celery tasks for database snapshotting and retention in GCS."""

import logging
import os
import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from . import storage_util

logger = logging.getLogger(__name__)

BACKUP_BASENAME_PATTERN = re.compile(r"db-(?P<ts>\d{8}T\d{6}Z)\.sqlite3$")


def _get_backup_bucket_name() -> Optional[str]:
    return getattr(settings, "DB_SNAPSHOT_BUCKET_NAME", None) or None


def _resolve_backup_prefix() -> str:
    prefix = getattr(settings, "DB_SNAPSHOT_PREFIX", "db-snapshots") or "db-snapshots"
    prefix = prefix.strip("/")
    return storage_util._apply_prefix(f"{prefix}/")  # type: ignore[attr-defined]


def _iter_backups(bucket, prefix: str) -> Iterable[Tuple[str, timezone.datetime]]:
    for blob in bucket.list_blobs(prefix=prefix):
        name = blob.name
        match = BACKUP_BASENAME_PATTERN.search(Path(name).name)
        if not match:
            continue
        try:
            ts = timezone.datetime.strptime(match.group("ts"), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            logger.debug("Skipping snapshot with unparseable timestamp: %s", name)
            continue
        yield name, ts


def _select_backups_to_delete(
    backups: List[Tuple[str, timezone.datetime]],
    *,
    now: timezone.datetime,
    hourly_hours: int,
    daily_days: int,
    weekly_weeks: int,
) -> List[str]:
    daily: Dict[timezone.date, Tuple[str, timezone.datetime]] = {}
    weekly: Dict[Tuple[int, int], Tuple[str, timezone.datetime]] = {}
    monthly: Dict[Tuple[int, int], Tuple[str, timezone.datetime]] = {}
    keep: set[str] = set()

    for name, ts in backups:
        age = now - ts
        if age <= timedelta(hours=hourly_hours):
            keep.add(name)
            continue
        if age <= timedelta(days=daily_days):
            key = ts.date()
            if key not in daily or daily[key][1] < ts:
                daily[key] = (name, ts)
            continue
        if age <= timedelta(weeks=weekly_weeks):
            iso_year, iso_week, _ = ts.isocalendar()
            key = (iso_year, iso_week)
            if key not in weekly or weekly[key][1] < ts:
                weekly[key] = (name, ts)
            continue
        month_key = (ts.year, ts.month)
        if month_key not in monthly or monthly[month_key][1] < ts:
            monthly[month_key] = (name, ts)

    for collection in (daily, weekly, monthly):
        keep.update(name for name, _ in collection.values())

    to_delete = [name for name, _ in backups if name not in keep]
    return to_delete


def prune_old_backups(now: Optional[timezone.datetime] = None) -> List[str]:
    bucket_name = _get_backup_bucket_name()
    if not bucket_name:
        logger.warning("DB snapshot pruning skipped: bucket not configured")
        return []
    bucket = storage_util.get_bucket(bucket_name)
    prefix = _resolve_backup_prefix()
    now = now or timezone.now()

    backups = sorted(_iter_backups(bucket, prefix), key=lambda item: item[1], reverse=True)
    to_delete = _select_backups_to_delete(
        backups,
        now=now,
        hourly_hours=getattr(settings, "DB_SNAPSHOT_RETENTION_HOURLY_HOURS", 24),
        daily_days=getattr(settings, "DB_SNAPSHOT_RETENTION_DAILY_DAYS", 7),
        weekly_weeks=getattr(settings, "DB_SNAPSHOT_RETENTION_WEEKLY_WEEKS", 8),
    )

    deleted: List[str] = []
    for name in to_delete:
        try:
            bucket.blob(name).delete()
            deleted.append(name)
        except Exception:
            logger.exception("Failed to delete old snapshot %s", name)

    if deleted:
        logger.info("Deleted %s old database snapshots", len(deleted))
    else:
        logger.info("No database snapshots deleted; retention already satisfied")
    return deleted


@shared_task(bind=True, name="storage.backup_tasks.snapshot_sqlite_database")
def snapshot_sqlite_database(self) -> Optional[str]:
    db_name = settings.DATABASES.get("default", {}).get("NAME")
    if not db_name:
        logger.warning("DB snapshot skipped: DATABASES['default']['NAME'] is not set")
        return None

    db_path = Path(db_name)
    if not db_path.exists():
        logger.warning("DB snapshot skipped: database file does not exist at %s", db_path)
        return None

    timestamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    object_name = f"{getattr(settings, 'DB_SNAPSHOT_PREFIX', 'db-snapshots').rstrip('/')}/db-{timestamp}.sqlite3"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3") as tmp:
            shutil.copy2(db_path, tmp.name)
            tmp_path = tmp.name
        upload_result = storage_util.upload_local_file(
            tmp_path,
            destination=object_name,
            bucket_name=_get_backup_bucket_name(),
        )
        logger.info(
            "Uploaded database snapshot to %s",
            upload_result.get("gs_uri") or upload_result.get("object_name"),
        )
    except Exception:
        logger.exception("Failed to upload database snapshot")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                logger.warning("Temporary snapshot file could not be removed: %s", tmp_path)

    try:
        prune_old_backups()
    except Exception:
        logger.exception("Database snapshot retention pruning failed")

    return object_name
