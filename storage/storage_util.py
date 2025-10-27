"""GCS storage utility helpers.

This module provides thin wrappers around google-cloud-storage for common
operations the app needs:

- Create a GCS client using settings or environment variables
- Upload from file-like objects, bytes, or local files
- Generate V4 signed URLs
- Build public URLs
- Check for and delete objects

Configuration can be set either in Django settings or environment variables:

- settings.GCS_BUCKET_NAME or env GCS_BUCKET_NAME
- settings.GCS_CREDENTIALS_FILE or env GCS_CREDENTIALS_FILE (path to JSON)
- settings.GCP_PROJECT or env GCP_PROJECT
- settings.GCS_PUBLIC_BASE_URL or env GCS_PUBLIC_BASE_URL (optional CDN base)
- settings.GCS_SIGNED_URL_TTL or env GCS_SIGNED_URL_TTL (seconds; default 3600)
- settings.GCS_UPLOAD_PREFIX or env GCS_UPLOAD_PREFIX (optional key prefix)

If no credentials path is provided, Application Default Credentials are used
via google-cloud-storage.
"""

from __future__ import annotations

import io
import os
import mimetypes
from datetime import timedelta
from typing import BinaryIO, Dict, Optional

try:
    from django.conf import settings  # type: ignore
except Exception:  # pragma: no cover - optional import for non-Django contexts
    class _Dummy:
        pass

    settings = _Dummy()  # type: ignore

from google.cloud import storage
from google.oauth2 import service_account


def _get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    # Prefer Django settings; fall back to environment.
    val = getattr(settings, name, None)
    if val is not None:
        return str(val)
    return os.getenv(name, default)


def _normalize_object_name(name: str) -> str:
    # Normalize path separators and strip leading slashes.
    name = name.replace("\\", "/")
    return name.lstrip("/")


def _apply_prefix(name: str) -> str:
    prefix = _get_setting("GCS_UPLOAD_PREFIX", "") or ""
    if prefix:
        prefix = _normalize_object_name(prefix)
        if not prefix.endswith("/"):
            prefix += "/"
    return f"{prefix}{_normalize_object_name(name)}"


def get_gcs_client() -> storage.Client:
    """Create a GCS client using settings or ADC.

    - If GCS_CREDENTIALS_FILE is provided, loads that service account.
    - Otherwise uses Application Default Credentials.
    """
    cred_path = _get_setting("GCS_CREDENTIALS_FILE")
    project = _get_setting("GCP_PROJECT")

    if cred_path:
        creds = service_account.Credentials.from_service_account_file(cred_path)
        return storage.Client(project=project, credentials=creds)
    # ADC: honors GOOGLE_APPLICATION_CREDENTIALS or ambient metadata
    if project:
        return storage.Client(project=project)
    return storage.Client()


def get_bucket(bucket_name: Optional[str] = None) -> storage.Bucket:
    name = bucket_name or _get_setting("GCS_BUCKET_NAME")
    if not name:
        raise ValueError("GCS bucket name is not configured (GCS_BUCKET_NAME)")
    client = get_gcs_client()
    return client.bucket(name)


def build_public_url(object_name: str, bucket_name: Optional[str] = None) -> str:
    """Build a public URL for an object.

    If GCS_PUBLIC_BASE_URL is set, it is used as the base (e.g., a CDN).
    Otherwise defaults to https://storage.googleapis.com/{bucket}/{object}.
    """
    bucket = bucket_name or _get_setting("GCS_BUCKET_NAME")
    if not bucket:
        raise ValueError("GCS bucket name is not configured (GCS_BUCKET_NAME)")
    base = _get_setting("GCS_PUBLIC_BASE_URL")
    object_name = _normalize_object_name(object_name)
    if base:
        return f"{base.rstrip('/')}/{object_name}"
    return f"https://storage.googleapis.com/{bucket}/{object_name}"


def _guess_content_type(object_name: str, fallback: str = "application/octet-stream") -> str:
    ctype, _ = mimetypes.guess_type(object_name)
    return ctype or fallback


def upload_fileobj(
    fileobj: BinaryIO,
    destination: str,
    *,
    bucket_name: Optional[str] = None,
    content_type: Optional[str] = None,
    make_public: bool = False,
    cache_control: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Upload a file-like object to GCS.

    Accepts any file-like object opened in binary mode (e.g., Django UploadedFile).

    Returns a dict with: bucket, object_name, gs_uri, public_url (if make_public).
    """
    bucket = get_bucket(bucket_name)
    object_name = _apply_prefix(destination)
    blob = bucket.blob(object_name)

    blob.cache_control = cache_control
    if metadata:
        blob.metadata = metadata

    ctype = content_type or _guess_content_type(object_name)

    # Ensure file pointer is at start; upload_from_file may not rewind by default.
    try:
        fileobj.seek(0)
    except Exception:
        pass

    blob.upload_from_file(fileobj, content_type=ctype, rewind=True)

    if make_public:
        blob.make_public()

    result = {
        "bucket": blob.bucket.name,
        "object_name": object_name,
        "gs_uri": f"gs://{blob.bucket.name}/{object_name}",
    }
    if make_public:
        result["public_url"] = blob.public_url
    return result


def upload_bytes(
    data: bytes,
    destination: str,
    **kwargs,
) -> Dict[str, str]:
    """Upload raw bytes to GCS."""
    return upload_fileobj(io.BytesIO(data), destination, **kwargs)


def upload_local_file(
    path: str,
    destination: Optional[str] = None,
    *,
    bucket_name: Optional[str] = None,
    content_type: Optional[str] = None,
    make_public: bool = False,
    cache_control: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Upload a local file path to GCS."""
    if destination is None:
        destination = os.path.basename(path)
    bucket = get_bucket(bucket_name)
    object_name = _apply_prefix(destination)
    blob = bucket.blob(object_name)

    blob.cache_control = cache_control
    if metadata:
        blob.metadata = metadata

    ctype = content_type or _guess_content_type(object_name)
    blob.upload_from_filename(path, content_type=ctype)

    if make_public:
        blob.make_public()

    result = {
        "bucket": blob.bucket.name,
        "object_name": object_name,
        "gs_uri": f"gs://{blob.bucket.name}/{object_name}",
    }
    if make_public:
        result["public_url"] = blob.public_url
    return result


def generate_signed_url(
    object_name: str,
    *,
    bucket_name: Optional[str] = None,
    expiration_seconds: Optional[int] = None,
    method: str = "GET",
    content_type: Optional[str] = None,
    response_disposition: Optional[str] = None,
) -> str:
    """Generate a V4 signed URL for an object."""
    ttl = expiration_seconds
    if ttl is None:
        # settings may provide as int or string
        ttl_setting = _get_setting("GCS_SIGNED_URL_TTL")
        try:
            ttl = int(ttl_setting) if ttl_setting is not None else 3600
        except ValueError:
            ttl = 3600

    bucket = get_bucket(bucket_name)
    object_name = _apply_prefix(object_name)
    blob = bucket.blob(object_name)

    params = {}
    if response_disposition:
        params["response_disposition"] = response_disposition

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=int(ttl)),
        method=method,
        content_type=content_type,
        query_parameters=params if params else None,
    )
    return url


def object_exists(object_name: str, *, bucket_name: Optional[str] = None) -> bool:
    bucket = get_bucket(bucket_name)
    object_name = _apply_prefix(object_name)
    blob = bucket.blob(object_name)
    return blob.exists()


def delete_object(object_name: str, *, bucket_name: Optional[str] = None) -> None:
    bucket = get_bucket(bucket_name)
    object_name = _apply_prefix(object_name)
    blob = bucket.blob(object_name)
    # Will raise NotFound if it doesn't exist; callers may wish to ignore.
    blob.delete()


def download_file_as_string(
    object_name: str,
    *,
    bucket_name: Optional[str] = None,
) -> str:
    """Download a GCS object and return its content as a string."""
    bucket = get_bucket(bucket_name)
    object_name = _apply_prefix(object_name)
    blob = bucket.blob(object_name)
    content = blob.download_as_text()
    return content


__all__ = [
    "get_gcs_client",
    "get_bucket",
    "upload_fileobj",
    "upload_bytes",
    "upload_local_file",
    "generate_signed_url",
    "build_public_url",
    "object_exists",
    "delete_object",
    "download_file_as_string",
]

