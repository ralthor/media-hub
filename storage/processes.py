from __future__ import annotations

from typing import Dict, Optional

from django.utils.text import get_valid_filename

from . import storage_util


def upload_to_gcs_and_sign(
    uploaded_file,
    *,
    object_name: Optional[str] = None,
    bucket_name: Optional[str] = None,
    expires_seconds: int = 24 * 60 * 60,
) -> Dict[str, str]:
    """Upload a Django UploadedFile to GCS and return details with a signed URL.

    - Uses storage_util.upload_fileobj for the upload
    - Generates a V4 signed URL valid for ``expires_seconds`` (default 24h)

    Returns a dict containing at least: bucket, object_name, gs_uri, signed_url
    """
    if object_name is None:
        object_name = get_valid_filename(getattr(uploaded_file, "name", "uploaded"))

    content_type = getattr(uploaded_file, "content_type", None)

    result = storage_util.upload_fileobj(
        uploaded_file,
        object_name,
        bucket_name=bucket_name,
        content_type=content_type,
    )

    signed_url = storage_util.generate_signed_url(
        result["object_name"],
        bucket_name=result["bucket"],
        expiration_seconds=expires_seconds,
        method="GET",
        content_type=None,
    )

    result["signed_url"] = signed_url
    return result
