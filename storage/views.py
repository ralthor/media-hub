import logging
from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from dataclasses import asdict

from . import storage_util
from .models import StoredFile
from .upload_helpers import (
    classify_uploaded_file,
    determine_storage_target,
    resolve_local_destination,
    schedule_primary_upload,
    write_upload_to_disk,
)

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

        classification = classify_uploaded_file(uploaded)
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
        object_name, file_uuid = determine_storage_target(
            request.user.id,
            classification,
        )

        stored_file = StoredFile.objects.create(
            user=request.user,
            file_uuid=file_uuid if classification.is_video else None,
            bucket_name=bucket_name or '',
            folder=object_name,
            size_bytes=0,
            original_filename=getattr(uploaded, 'name', classification.filename),
            content_type=classification.effective_content_type or '',
        )

        # First write to local filesystem (MEDIA_ROOT or temp dir)
        _, dest_path = resolve_local_destination(
            classification.filename,
            base_dir=stored_file.local_workdir,
        )

        logger.info("upload_file: writing local file to %s", dest_path)
        try:
            total_written = write_upload_to_disk(uploaded, dest_path)
            logger.info(
                "upload_file: local write complete bytes=%s (reported size=%s)",
                total_written,
                getattr(uploaded, 'size', 'unknown'),
            )
        except OSError as e:
            logger.exception("upload_file: local write failed: %s", e)
            stored_file.advance_status(StoredFile.Status.ERROR)
            context['error'] = f'Failed to write file: {e}'
            return render(request, 'upload.html', context, status=500)

        stored_file.size_bytes = total_written
        stored_file.save(update_fields=['size_bytes'])

        # Queue the file for upload to GCS via Celery worker
        try:
            task_result = schedule_primary_upload.delay(
                stored_file_id=stored_file.id,
                local_path=dest_path,
                classification=asdict(classification),
                object_name=object_name,
                bucket_name=bucket_name,
                size_bytes=total_written,
                original_filename=getattr(uploaded, 'name', classification.filename),
            )
        except Exception as e:
            logger.exception("upload_file: failed to enqueue primary upload task: %s", e)
            stored_file.advance_status(StoredFile.Status.ERROR)
            context['error'] = 'Failed to enqueue background upload task.'
            return render(request, 'upload.html', context, status=500)

        logger.info(
            "upload_file: queued primary upload task task_id=%s path=%s object=%s bucket=%s",
            getattr(task_result, 'id', None),
            dest_path,
            object_name,
            bucket_name,
        )

        context['success'] = f'Uploaded to {dest_path}'
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
                'status': stored.status,
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
