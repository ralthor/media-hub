from __future__ import annotations

import tempfile
from pathlib import Path
from typing import ClassVar, Dict, Iterable

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models


class StoredFile(models.Model):
    """Stores metadata about an uploaded file stored in object storage.

    Notes
    - `file_uuid` is primarily used for video uploads and remains optional.
    - Storage location split into `bucket_name` and `folder` for portability.
    """

    class Status(models.TextChoices):
        UPLOADING = "UPLOADING", "Uploading"
        UPLOAD_COMPLETE = "UPLOAD_COMPLETE", "Upload Complete"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSING_HLS = "PROCESSING_HLS", "Processing HLS"
        READY = "READY", "Ready"
        ERROR = "ERROR", "Error"
        DELETING = "DELETING", "Deleting"

    _STATUS_ORDER: ClassVar[Dict[str, int]] = {
        Status.UPLOADING: 10,
        Status.UPLOAD_COMPLETE: 20,
        Status.PROCESSING: 30,
        Status.PROCESSING_HLS: 40,
        Status.DELETING: 45,
        Status.READY: 50,
        Status.ERROR: 60,
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stored_files",
    )
    file_uuid = models.UUIDField(null=True, blank=True, unique=True)

    bucket_name = models.CharField(max_length=255)
    folder = models.CharField(max_length=1024, blank=True, default="")

    size_bytes = models.BigIntegerField()
    uploaded_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    original_filename = models.CharField(max_length=512, blank=True, default="")
    content_type = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.UPLOADING,
        editable=False,
    )

    class Meta:
        indexes = [
            models.Index(fields=["file_uuid"], name="storedfile_uuid_idx"),
            models.Index(fields=["bucket_name", "folder"], name="storedfile_loc_idx"),
            models.Index(fields=["user", "-uploaded_at"], name="storedfile_user_idx"),
            models.Index(
                fields=["user", "deleted_at", "-uploaded_at"],
                name="storedfile_user_deleted_idx",
            ),
        ]
        ordering = ["-uploaded_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.bucket_name]
        if self.folder:
            parts.append(self.folder)
        path = "/".join(parts)
        return f"StoredFile(id={self.pk}, uuid={self.file_uuid}, path={path}, size={self.size_bytes})"

    @property
    def storage_path(self) -> str:
        """Convenience: bucket/folder path (no filename)."""
        return f"{self.bucket_name}/{self.folder}" if self.folder else self.bucket_name

    @property
    def per_upload_prefix(self) -> str:
        """Return the object storage prefix for artifacts of this upload."""
        if self.file_uuid and self.folder:
            # Video uploads keep the primary payload at .../<uuid>/file
            base, _, leaf = self.folder.rpartition("/")
            if leaf == "file":
                return base
        if self.folder:
            base, _, _ = self.folder.rpartition("/")
            return base or self.folder
        return ""

    @property
    def local_workdir(self) -> str:
        """Return a dedicated local temp directory for this upload."""
        media_root = getattr(settings, "MEDIA_ROOT", "") or tempfile.gettempdir()
        base = Path(media_root) / "temp" / "uploads"
        suffix = str(self.file_uuid) if self.file_uuid else f"id-{self.pk or 'pending'}"
        return str(base / suffix)

    @classmethod
    def status_rank(cls, status: str) -> int:
        return cls._STATUS_ORDER.get(status, -1)

    @classmethod
    def _allowed_statuses_for(cls, target_status: str) -> Iterable[str]:
        target_rank = cls.status_rank(target_status)
        return [status for status, rank in cls._STATUS_ORDER.items() if rank <= target_rank]

    def advance_status(
        self,
        target_status: str,
        *,
        allow_from: Iterable[str] | None = None,
    ) -> bool:
        """Update to the target status if it does not regress.

        `allow_from` explicitly whitelists current statuses that may transition
        even if they are normally considered further along the lifecycle.
        """
        if target_status not in self.Status.values:
            raise ValueError(f"Invalid status transition to {target_status!r}")
        allowed = set(self._allowed_statuses_for(target_status))
        if allow_from:
            allowed.update(allow_from)
        updated = (
            StoredFile.objects.filter(pk=self.pk, status__in=allowed)
            .update(status=target_status)
        )
        if updated:
            self.status = target_status
            return True
        self.refresh_from_db(fields=["status"])
        return self.status == target_status


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The email must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    # Remove the username field and use email as the unique identifier
    username = None
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.email
