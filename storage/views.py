import logging
import mimetypes
import os
import tempfile
import uuid

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import get_valid_filename

from . import processes
from . import storage_util
from .models import StoredFile

logger = logging.getLogger(__name__)


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

        filename = get_valid_filename(os.path.basename(uploaded.name))
        logger.info("upload_file: sanitized filename=%s", filename)
        raw_content_type = getattr(uploaded, 'content_type', '') or ''
        guessed_type, _ = mimetypes.guess_type(filename)
        file_ext = os.path.splitext(filename)[1].lower()
        content_type = (raw_content_type or guessed_type or '').lower()
        video_exts = {
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
        image_exts = {
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
        is_video = False
        if content_type.startswith('video/'):
            is_video = True
        elif guessed_type and guessed_type.lower().startswith('video/'):
            is_video = True
        elif file_ext in video_exts:
            is_video = True
        is_photo = False
        if content_type.startswith('image/'):
            is_photo = True
        elif guessed_type and guessed_type.lower().startswith('image/'):
            is_photo = True
        elif file_ext in image_exts:
            is_photo = True
        if is_video:
            is_photo = False  # Prefer video classification if both heuristics match
        file_category = 'video' if is_video else ('photo' if is_photo else 'file')
        effective_content_type = raw_content_type or guessed_type or ''
        if effective_content_type.lower() == 'application/octet-stream' and guessed_type:
            effective_content_type = guessed_type
        logger.info(
            "upload_file: classified file category=%s video=%s photo=%s content_type=%s ext=%s",
            file_category,
            is_video,
            is_photo,
            effective_content_type or 'unknown',
            file_ext or '(none)',
        )
        bucket_name = getattr(settings, 'GCS_BUCKET_NAME', None)
        padded_user_id = f"{request.user.id:05d}"
        file_uuid = uuid.uuid4() if is_video else None
        if is_video:
            object_name = f"user/{padded_user_id}/{file_uuid}/file"
        elif is_photo:
            object_name = f"user/{padded_user_id}/photos/{filename}"
        else:
            object_name = f"user/{padded_user_id}/files/{filename}"

        # First write to local filesystem (MEDIA_ROOT or temp dir)
        dest_dir = getattr(settings, 'MEDIA_ROOT', None) or tempfile.gettempdir()
        dest_dir = str(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)

        logger.info("upload_file: writing local file to %s", dest_path)
        try:
            total_written = 0
            with open(dest_path, 'wb') as out:
                for chunk in uploaded.chunks():
                    out.write(chunk)
                    total_written += len(chunk)
            logger.info(
                "upload_file: local write complete bytes=%s (reported size=%s)",
                total_written,
                getattr(uploaded, 'size', 'unknown'),
            )
        except OSError as e:
            logger.exception("upload_file: local write failed: %s", e)
            context['error'] = f'Failed to write file: {e}'
            return render(request, 'upload.html', context, status=500)

        # Then upload the file to GCS and generate a signed URL
        try:
            logger.info("upload_file: starting GCS upload object=%s", filename)
            res = processes.upload_to_gcs_and_sign(
                uploaded,
                object_name=object_name,
                bucket_name=bucket_name,
                expires_seconds=24 * 60 * 60,
            )
        except Exception as e:
            logger.exception("upload_file: GCS upload failed: %s", e)
            context['error'] = f'Failed to upload to storage: {e}'
            return render(request, 'upload.html', context, status=500)

        stored_object_name = res.get('object_name') or object_name
        resolved_bucket = res.get('bucket') or bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)
        if resolved_bucket:
            logger.info(
                "upload_file: recording file metadata user=%s category=%s bucket=%s object=%s",
                request.user.id,
                file_category,
                resolved_bucket,
                stored_object_name,
            )
            StoredFile.objects.create(
                user=request.user,
                file_uuid=file_uuid if is_video else None,
                bucket_name=resolved_bucket,
                folder=stored_object_name,
                size_bytes=total_written,
                original_filename=getattr(uploaded, 'name', ''),
                content_type=effective_content_type or '',
            )
        else:
            logger.warning(
                "upload_file: skipping metadata persistence due to missing bucket user=%s object=%s",
                request.user.id,
                stored_object_name,
            )
        context['success'] = f'Uploaded to {dest_path}'
        gs_uri = res.get('gs_uri') or f"gs://{res.get('bucket')}/{stored_object_name}"
        logger.info("upload_file: GCS upload complete gs_uri=%s", gs_uri)
        context['signed_url'] = res.get('signed_url')
        context['object_name'] = stored_object_name
        context['bucket'] = res.get('bucket')
        context['gcs_uri'] = gs_uri
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
