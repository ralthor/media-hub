import logging
import mimetypes
import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
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


def _classify_uploaded_file(uploaded) -> UploadClassification:
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
        is_photo = False  # Prefer video classification if both heuristics match

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


def _determine_storage_target(user_id: int, classification: UploadClassification) -> Tuple[str, Optional[uuid.UUID]]:
    padded_user_id = f"{user_id:05d}"
    file_uuid = uuid.uuid4() if classification.is_video else None
    if classification.is_video:
        object_name = f"user/{padded_user_id}/{file_uuid}/file"
    elif classification.is_photo:
        object_name = f"user/{padded_user_id}/photos/{classification.filename}"
    else:
        object_name = f"user/{padded_user_id}/files/{classification.filename}"
    return object_name, file_uuid


def _resolve_local_destination(filename: str) -> Tuple[str, str]:
    dest_dir = getattr(settings, 'MEDIA_ROOT', None) or tempfile.gettempdir()
    dest_dir = str(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    return dest_dir, dest_path


def _write_upload_to_disk(uploaded, dest_path: str) -> int:
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


def _schedule_primary_upload(
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
        bucket_name or '(default)',
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


@login_required
def video_page(request: HttpRequest) -> HttpResponse:
    # Read manifest content from GCS and pass it to the template context.
    manifest_object_name = 'output.m3u8'  # Example manifest file in GCS
    bucket_name = getattr(settings, 'GCS_BUCKET_NAME', None)
    logger.info(
        "video_page: fetching manifest object=%s bucket=%s",
        manifest_object_name,
        bucket_name,
    )
    content = storage_util.download_file_as_string(
        manifest_object_name,
        bucket_name=bucket_name,
    )
    logger.info(
        "video_page: fetched manifest length=%s characters",
        len(content) if content is not None else 0,
    )
    context = {
        'manifest_content': content,
    }
    logger.info("video_page: rendering template with manifest")
    return render(request, 'video.html', context)


@login_required
def upload_file(request: HttpRequest) -> HttpResponse:
    logger.info("upload_file: received %s request", request.method)
    if request.method == 'POST':
        uploaded = request.FILES.get('file')
        context = {}
        if not uploaded:
            logger.warning("upload_file: no file provided in POST")
            context['error'] = 'No file provided.'
            return render(request, 'upload.html', context, status=400)

        classification = _classify_uploaded_file(uploaded)
        logger.info("upload_file: sanitized filename=%s", classification.filename)
        logger.info(
            "upload_file: classified file category=%s video=%s photo=%s content_type=%s ext=%s",
            classification.file_category,
            classification.is_video,
            classification.is_photo,
            classification.effective_content_type or 'unknown',
            classification.file_ext or '(none)',
        )
        bucket_name = getattr(settings, 'GCS_BUCKET_NAME', None)
        object_name, file_uuid = _determine_storage_target(
            request.user.id,
            classification,
        )

        # First write to local filesystem (MEDIA_ROOT or temp dir)
        _, dest_path = _resolve_local_destination(classification.filename)

        logger.info("upload_file: writing local file to %s", dest_path)
        try:
            total_written = _write_upload_to_disk(uploaded, dest_path)
            logger.info(
                "upload_file: local write complete bytes=%s (reported size=%s)",
                total_written,
                getattr(uploaded, 'size', 'unknown'),
            )
        except OSError as e:
            logger.exception("upload_file: local write failed: %s", e)
            context['error'] = f'Failed to write file: {e}'
            return render(request, 'upload.html', context, status=500)

        # Then upload the file to GCS (synchronously for now)
        try:
            _schedule_primary_upload(
                uploaded=uploaded,
                local_path=dest_path,
                classification=classification,
                object_name=object_name,
                bucket_name=bucket_name,
                user=request.user,
                file_uuid=file_uuid,
                size_bytes=total_written,
                context=context,
            )
        except Exception as e:
            logger.exception("upload_file: GCS upload failed: %s", e)
            context['error'] = f'Failed to upload to storage: {e}'
            return render(request, 'upload.html', context, status=500)

        context['success'] = f'Uploaded to {dest_path}'
        if context.get('gcs_uri'):
            logger.info("upload_file: GCS upload complete gs_uri=%s", context['gcs_uri'])
        else:
            logger.info("upload_file: GCS upload complete with unknown gs_uri")
        logger.info("upload_file: returning success response")
        return render(request, 'upload.html', context)

    # GET -> render simple upload form
    logger.info("upload_file: rendering upload form (GET)")
    return render(request, 'upload.html')


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    logger.info("dashboard: fetching uploaded files for user=%s", request.user.id)
    user_files = StoredFile.objects.filter(user=request.user).order_by('-uploaded_at')
    uploaded_files = []
    for stored in user_files:
        category = 'file'
        if stored.content_type.startswith('video/'):
            category = 'video'
        elif stored.content_type.startswith('image/'):
            category = 'photo'
        uploaded_files.append(
            {
                'id': stored.id,
                'bucket': stored.bucket_name,
                'object_name': stored.folder,
                'original_filename': stored.original_filename,
                'uploaded_at': stored.uploaded_at,
                'content_type': stored.content_type,
                'category': category,
                'file_uuid': stored.file_uuid,
            }
        )
    context = {
        'uploaded_files': uploaded_files,
    }
    return render(request, 'dashboard.html', context)


def logout_view(request: HttpRequest) -> HttpResponse:
    """Log the user out via GET and redirect safely.

    Needed because Django 5 defaults to POST-only logout.
    Our tests and some UX flows expect GET support with `next`.
    """
    logout(request)
    next_url = request.GET.get('next') or getattr(settings, 'LOGOUT_REDIRECT_URL', '/') or '/'
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = '/'
    return redirect(next_url)
