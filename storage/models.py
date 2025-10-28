from __future__ import annotations

import uuid as _uuid
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.base_user import BaseUserManager


class Video(models.Model):
    """Stores metadata about an uploaded video stored in object storage.

    Notes
    - `file_uuid` will be assigned on upload; kept nullable for pre-create.
    - Storage location split into `bucket_name` and `folder` for portability.
    """

    # Assigned later during upload flow; kept nullable until set
    file_uuid = models.UUIDField(null=True, blank=True, unique=True)

    # Storage location
    bucket_name = models.CharField(max_length=255)
    folder = models.CharField(max_length=1024, blank=True, default="")

    # File details
    size_bytes = models.BigIntegerField()
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # Optional metadata
    original_filename = models.CharField(max_length=512, blank=True, default="")
    content_type = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["file_uuid"], name="video_uuid_idx"),
            models.Index(fields=["bucket_name", "folder"], name="video_loc_idx"),
        ]
        ordering = ["-uploaded_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.bucket_name]
        if self.folder:
            parts.append(self.folder)
        path = "/".join(parts)
        return f"Video(id={self.pk}, uuid={self.file_uuid}, path={path}, size={self.size_bytes})"

    @property
    def storage_path(self) -> str:
        """Convenience: bucket/folder path (no filename)."""
        return f"{self.bucket_name}/{self.folder}" if self.folder else self.bucket_name


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
