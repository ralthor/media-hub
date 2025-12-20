"""Celery task registrations for the storage app."""

# Import tasks so Celery autodiscovery finds them when loading this module.
from .backup_tasks import snapshot_sqlite_database  # noqa: F401
