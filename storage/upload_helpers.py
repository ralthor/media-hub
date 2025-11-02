import logging
import mimetypes
import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from django.conf import settings
from django.utils.text import get_valid_filename

from . import storage_util
from .models import StoredFile

logger = logging.getLogger(__name__)


VIDEO_EXTENSIONS = {
    '.3g2',
    '.3gp',
    '.avi',
    '.flv',
    '.m2ts',
    '.m4v',
    '.mkv',
    '.mov',
    '.mp4',
    '.mpeg',
    '.mpg',
    '.mts',
    '.ogv',
    '.ts',
    '.webm',
    '.wmv',
}

IMAGE_EXTENSIONS = {
    '.bmp',
    '.gif',
    '.heic',
    '.jpeg',
    '.jpg',
    '.png',
    '.svg',
    '.tif',
    '.tiff',
    '.webp',
}


@dataclass
class UploadClassification:
    filename: str
    effective_content_type: str
    file_category: str
    is_video: bool
    is_photo: bool
    file_ext: str


def classify_uploaded_file(uploaded) -> UploadClassification:
    filename = get_valid_filename(os.path.basename(uploaded.name))
    raw_content_type = getattr(uploaded, 'content_type', '') or ''
    guessed_type, _ = mimetypes.guess_type(filename)
    file_ext = os.path.splitext(filename)[1].lower()
    content_type = (raw_content_type or guessed_type or '').lower()

    is_video = False
    if content_type.startswith('video/'):
        is_video = True
    elif guessed_type and guessed_type.lower().startswith('video/'):
        is_video = True
    elif file_ext in VIDEO_EXTENSIONS:
        is_video = True

    is_photo = False
    if content_type.startswith('image/'):
        is_photo = True
    elif guessed_type and guessed_type.lower().startswith('image/'):
        is_photo = True
    elif file_ext in IMAGE_EXTENSIONS:
        is_photo = True

    if is_video:
        is_photo = False

    effective_content_type = raw_content_type or guessed_type or ''
    if effective_content_type.lower() == 'application/octet-stream' and guessed_type:
        effective_content_type = guessed_type

    file_category = 'video' if is_video else ('photo' if is_photo else 'file')

    return UploadClassification(
        filename=filename,
        effective_content_type=effective_content_type or '',
        file_category=file_category,
        is_video=is_video,
        is_photo=is_photo,
        file_ext=file_ext or '',
    )


def determine_storage_target(user_id: int, classification: UploadClassification) -> Tuple[str, Optional[uuid.UUID]]:
    padded_user_id = f"{user_id:05d}"
    file_uuid = uuid.uuid4() if classification.is_video else None
    if classification.is_video:
        object_name = f"user/{padded_user_id}/{file_uuid}/file"
    elif classification.is_photo:
        object_name = f"user/{padded_user_id}/photos/{classification.filename}"
    else:
        object_name = f"user/{padded_user_id}/files/{classification.filename}"
    return object_name, file_uuid


def resolve_local_destination(filename: str) -> Tuple[str, str]:
    dest_dir = getattr(settings, 'MEDIA_ROOT', None) or tempfile.gettempdir()
    dest_dir = str(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    return dest_dir, dest_path


def write_upload_to_disk(uploaded, dest_path: str) -> int:
    total_written = 0
    with open(dest_path, 'wb') as out:
        for chunk in uploaded.chunks():
            out.write(chunk)
            total_written += len(chunk)
    try:
        uploaded.seek(0)
    except (AttributeError, OSError):
        pass
    return total_written


def _persist_stored_file(
    *,
    user,
    stored_object_name: str,
    bucket_name: Optional[str],
    classification: UploadClassification,
    file_uuid: Optional[uuid.UUID],
    size_bytes: int,
    uploaded,
):
    return StoredFile.objects.create(
        user=user,
        file_uuid=file_uuid if classification.is_video else None,
        bucket_name=bucket_name or '',
        folder=stored_object_name,
        size_bytes=size_bytes,
        original_filename=getattr(uploaded, 'name', ''),
        content_type=classification.effective_content_type or '',
    )


def _schedule_video_processing(stored_file: StoredFile, local_path: str) -> None:
    if not stored_file.file_uuid:
        return
    logger.info(
        "upload_file: queued video processing placeholder uuid=%s path=%s",
        stored_file.file_uuid,
        local_path,
    )


def _schedule_local_cleanup(stored_file: StoredFile, local_path: str) -> None:
    logger.info(
        "upload_file: local cleanup pending for stored_file_id=%s path=%s",
        stored_file.id,
        local_path,
    )


def schedule_primary_upload(
    *,
    uploaded,
    local_path: str,
    classification: UploadClassification,
    object_name: str,
    bucket_name: Optional[str],
    user,
    file_uuid: Optional[uuid.UUID],
    size_bytes: int,
    context: Optional[dict] = None,
) -> None:
    logger.info(
        "upload_file: scheduling primary storage upload path=%s object=%s bucket=%s",
        local_path,
        object_name,
        bucket_name,
    )
    result = storage_util.upload_local_file(
        local_path,
        destination=object_name,
        bucket_name=bucket_name,
        content_type=classification.effective_content_type or None,
    )
    stored_object_name = result.get('object_name') or object_name
    resolved_bucket = result.get('bucket') or bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)

    stored_file = None
    if resolved_bucket:
        logger.info(
            "upload_file: recording file metadata user=%s category=%s bucket=%s object=%s",
            user.id,
            classification.file_category,
            resolved_bucket,
            stored_object_name,
        )
        stored_file = _persist_stored_file(
            user=user,
            stored_object_name=stored_object_name,
            bucket_name=resolved_bucket,
            classification=classification,
            file_uuid=file_uuid,
            size_bytes=size_bytes,
            uploaded=uploaded,
        )
    else:
        logger.warning(
            "upload_file: skipping metadata persistence due to missing bucket user=%s object=%s",
            user.id,
            stored_object_name,
        )

    if context is not None:
        context['object_name'] = stored_object_name
        if resolved_bucket:
            context['bucket'] = resolved_bucket
            context['gcs_uri'] = result.get('gs_uri') or f"gs://{resolved_bucket}/{stored_object_name}"
        else:
            context['bucket'] = None
            context['gcs_uri'] = None

    if stored_file:
        _schedule_video_processing(stored_file, local_path)
        _schedule_local_cleanup(stored_file, local_path)
