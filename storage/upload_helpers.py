import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from celery import shared_task

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


def resolve_local_destination(filename: str, *, base_dir: Optional[str] = None) -> Tuple[str, str]:
    dest_dir = base_dir or getattr(settings, 'MEDIA_ROOT', None) or tempfile.gettempdir()
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


def _classification_from_dict(payload: Dict[str, object]) -> UploadClassification:
    return UploadClassification(
        filename=str(payload.get('filename', '')),
        effective_content_type=str(payload.get('effective_content_type', '')),
        file_category=str(payload.get('file_category', '')),
        is_video=bool(payload.get('is_video', False)),
        is_photo=bool(payload.get('is_photo', False)),
        file_ext=str(payload.get('file_ext', '')),
    )


@shared_task(bind=False)
def _schedule_video_processing(stored_file_id: int, local_path: str) -> None:
    stored_file = (
        StoredFile.objects.filter(id=stored_file_id)
        .only('id', 'file_uuid', 'status')
        .first()
    )
    if not stored_file or not stored_file.file_uuid:
        logger.info(
            "upload_file: skipping video processing stored_file_id=%s (missing or no uuid)",
            stored_file_id,
        )
        return
    current_rank = StoredFile.status_rank(stored_file.status)
    if current_rank >= StoredFile.status_rank(StoredFile.Status.PROCESSING_HLS):
        logger.info(
            "upload_file: video processing already satisfied stored_file_id=%s status=%s",
            stored_file_id,
            stored_file.status,
        )
        _schedule_local_cleanup.delay(
            stored_file_id,
            stored_file.local_workdir,
            final_status=StoredFile.Status.READY,
        )
        return

    workdir = Path(stored_file.local_workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        stored_file.advance_status(StoredFile.Status.PROCESSING)
        hls_dir = workdir / "hls"
        hls_dir.mkdir(parents=True, exist_ok=True)
        stored_file.advance_status(StoredFile.Status.PROCESSING_HLS)

        segmented_dir = hls_dir / "segmented"
        if segmented_dir.exists():
            shutil.rmtree(segmented_dir)
        segmented_dir.mkdir(parents=True, exist_ok=True)
        playlist_name = "output.m3u8"
        playlist_path = segmented_dir / playlist_name
        segment_pattern = segmented_dir / "segment_%05d.ts"

        ffmpeg_path = shutil.which("ffmpeg")
        ffmpeg_cmd = [
            ffmpeg_path or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            local_path,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-g",
            "50",
            "-hls_time",
            "10",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(segment_pattern),
            "-f",
            "hls",
            str(playlist_path),
        ]
        segmentation_succeeded = False
        if ffmpeg_path:
            ffmpeg_cmd[0] = ffmpeg_path
            logger.info(
                "upload_file: starting ffmpeg segmentation stored_file_id=%s",
                stored_file_id,
            )
            try:
                subprocess.run(ffmpeg_cmd, check=True)
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "upload_file: ffmpeg failed stored_file_id=%s returncode=%s",
                    stored_file_id,
                    exc.returncode,
                    exc_info=True,
                )
            except FileNotFoundError:
                logger.warning(
                    "upload_file: ffmpeg binary missing stored_file_id=%s",
                    stored_file_id,
                )
            else:
                segmentation_succeeded = True
                logger.info(
                    "upload_file: completed ffmpeg segmentation stored_file_id=%s",
                    stored_file_id,
                )
        else:
            logger.warning(
                "upload_file: ffmpeg not found on PATH; skipping segmentation stored_file_id=%s",
                stored_file_id,
            )

        if segmentation_succeeded:
            bucket_name = stored_file.bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)
            if not bucket_name:
                raise ValueError("Bucket name missing; cannot upload segmented assets")
            base_prefix = stored_file.per_upload_prefix or stored_file.folder or ""
            base_prefix = base_prefix.strip("/")
            segmented_prefix = f"{base_prefix}/segmented" if base_prefix else "segmented"

            segment_uploads = []
            for root, _, files in os.walk(segmented_dir):
                for file_name in files:
                    local_file = Path(root) / file_name
                    rel_path = local_file.relative_to(segmented_dir).as_posix()
                    destination = f"{segmented_prefix}/{rel_path}" if rel_path else segmented_prefix
                    segment_uploads.append((local_file, destination))

            total_segments = len(segment_uploads)
            for index, (local_file, destination) in enumerate(segment_uploads, start=1):
                logger.info(
                    "upload_file: uploading segment stored_file_id=%s destination=%s (%s/%s files uploaded)",
                    stored_file_id,
                    destination,
                    index,
                    total_segments,
                )
                storage_util.upload_local_file(
                    str(local_file),
                    destination=destination,
                    bucket_name=bucket_name,
                )
            logger.info(
                "upload_file: completed uploading segmented assets stored_file_id=%s total_files=%s",
                stored_file_id,
                total_segments,
            )

    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "upload_file: failed video processing setup stored_file_id=%s error=%s",
            stored_file_id,
            exc,
        )
        stored_file.advance_status(StoredFile.Status.ERROR)
        return

    _schedule_local_cleanup.delay(
        stored_file_id,
        str(workdir),
        final_status=StoredFile.Status.READY,
    )


@shared_task(bind=False)
def _schedule_local_cleanup(
    stored_file_id: int,
    workdir: str,
    *,
    final_status: Optional[str] = None,
) -> None:
    stored_file = StoredFile.objects.filter(id=stored_file_id).only('id', 'status').first()
    if not stored_file:
        logger.info(
            "upload_file: cleanup skipped stored_file_id=%s (missing record)",
            stored_file_id,
        )
        return
    try:
        stored_file.advance_status(
            StoredFile.Status.DELETING,
            allow_from=[StoredFile.Status.READY],
        )
    except ValueError:  # pragma: no cover - defensive
        logger.exception(
            "upload_file: invalid cleanup transition stored_file_id=%s current=%s",
            stored_file_id,
            stored_file.status,
        )
        return

    path = Path(workdir)
    try:
        if path.exists():
            media_root = Path(getattr(settings, 'MEDIA_ROOT', tempfile.gettempdir())).resolve()
            try:
                path.resolve().relative_to(media_root)
            except ValueError:
                logger.warning(
                    "upload_file: refusing to delete outside media_root stored_file_id=%s path=%s",
                    stored_file_id,
                    path,
                )
            else:
                shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "upload_file: cleanup failed stored_file_id=%s error=%s",
            stored_file_id,
            exc,
        )
        stored_file.advance_status(StoredFile.Status.ERROR)
        return

    if final_status and final_status in StoredFile.Status.values:
        stored_file.advance_status(final_status)


@shared_task(bind=False)
def schedule_primary_upload(
    *,
    stored_file_id: int,
    local_path: str,
    classification: Dict[str, object],
    object_name: str,
    bucket_name: Optional[str],
    size_bytes: int,
    original_filename: str,
) -> None:
    classification_obj = _classification_from_dict(classification)
    stored_file = StoredFile.objects.filter(id=stored_file_id).first()
    if not stored_file:
        logger.warning(
            "upload_file: stored_file missing stored_file_id=%s; cannot upload",
            stored_file_id,
        )
        return

    workdir = Path(stored_file.local_workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(local_path):
        logger.error(
            "upload_file: expected local path missing stored_file_id=%s path=%s",
            stored_file_id,
            local_path,
        )
        stored_file.advance_status(StoredFile.Status.ERROR)
        return

    resolved_bucket = bucket_name or stored_file.bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)
    destination = object_name

    already_exists = False
    if stored_file.folder and stored_file.bucket_name:
        try:
            already_exists = storage_util.object_exists(
                stored_file.folder,
                bucket_name=stored_file.bucket_name,
            )
        except Exception:  # pragma: no cover - best-effort check
            already_exists = False

    try:
        if already_exists:
            logger.info(
                "upload_file: remote object already present stored_file_id=%s object=%s bucket=%s",
                stored_file_id,
                stored_file.folder,
                stored_file.bucket_name,
            )
            result = {
                'bucket': stored_file.bucket_name,
                'object_name': stored_file.folder,
            }
        else:
            result = storage_util.upload_local_file(
                local_path,
                destination=destination,
                bucket_name=resolved_bucket,
                content_type=classification_obj.effective_content_type or None,
            )
    except Exception as exc:
        logger.exception(
            "upload_file: failed primary upload stored_file_id=%s error=%s",
            stored_file_id,
            exc,
        )
        stored_file.advance_status(StoredFile.Status.ERROR)
        return

    stored_file.bucket_name = result.get('bucket') or resolved_bucket or stored_file.bucket_name
    stored_file.folder = result.get('object_name') or stored_file.folder or destination
    stored_file.size_bytes = size_bytes
    stored_file.original_filename = original_filename or classification_obj.filename
    stored_file.content_type = classification_obj.effective_content_type or ''
    stored_file.save(
        update_fields=[
            'bucket_name',
            'folder',
            'size_bytes',
            'original_filename',
            'content_type',
        ]
    )

    stored_file.advance_status(StoredFile.Status.UPLOAD_COMPLETE)

    video_processing_enabled = getattr(settings, 'VIDEO_PROCESSING_ENABLED', True)
    if classification_obj.is_video and stored_file.file_uuid and not video_processing_enabled:
        logger.info(
            "upload_file: video processing disabled stored_file_id=%s",
            stored_file.id,
        )
        _schedule_local_cleanup.delay(
            stored_file.id,
            stored_file.local_workdir,
            final_status=StoredFile.Status.READY,
        )
    elif classification_obj.is_video and stored_file.file_uuid:
        _schedule_video_processing.delay(stored_file.id, local_path)
    else:
        _schedule_local_cleanup.delay(
            stored_file.id,
            stored_file.local_workdir,
            final_status=StoredFile.Status.READY,
        )
