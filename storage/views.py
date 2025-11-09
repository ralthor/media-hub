import logging
import os
from dataclasses import asdict
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_POST

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


def _serialize_file(stored: StoredFile) -> dict:
    """Return template-friendly metadata describing a StoredFile."""
    ctype = (stored.content_type or '').lower()
    category = 'file'
    if ctype.startswith('video/'):
        category = 'video'
    elif ctype.startswith('image/'):
        category = 'photo'

    is_deleted = bool(stored.deleted_at)
    can_play = (
        category == 'video'
        and stored.file_uuid
        and stored.status == StoredFile.Status.READY
        and not is_deleted
    )

    return {
        'id': stored.id,
        'bucket': stored.bucket_name,
        'object_name': stored.folder,
        'original_filename': stored.original_filename,
        'uploaded_at': stored.uploaded_at,
        'content_type': stored.content_type,
        'category': category,
        'file_uuid': stored.file_uuid,
        'status': stored.status,
        'can_play': can_play,
        'is_deleted': is_deleted,
        'deleted_at': stored.deleted_at,
    }


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
    user_files = (
        StoredFile.objects.filter(user=request.user, deleted_at__isnull=True)
        .order_by('-uploaded_at')
    )
    uploaded_files = [_serialize_file(stored) for stored in user_files]
    deleted_total = (
        StoredFile.objects.filter(user=request.user, deleted_at__isnull=False)
        .count()
    )
    context = {
        'uploaded_files': uploaded_files,
        'deleted_total': deleted_total,
    }
    return render(request, 'dashboard.html', context)


@login_required
def video_library(request: HttpRequest) -> HttpResponse:
    logger.info("video_library: fetching videos for user=%s", request.user.id)
    videos = (
        StoredFile.objects.filter(user=request.user, deleted_at__isnull=True)
        .filter(content_type__startswith='video/')
        .order_by('-uploaded_at')
    )
    entries = [_serialize_file(stored) for stored in videos]
    context = {
        'videos': entries,
    }
    return render(request, 'videos.html', context)


@login_required
def bin_page(request: HttpRequest) -> HttpResponse:
    logger.info("bin_page: fetching deleted files for user=%s", request.user.id)
    deleted_files = (
        StoredFile.objects.filter(user=request.user, deleted_at__isnull=False)
        .order_by('-deleted_at', '-uploaded_at')
    )
    context = {
        'deleted_files': [_serialize_file(stored) for stored in deleted_files],
    }
    return render(request, 'bin.html', context)


@login_required
@require_POST
def delete_file(request: HttpRequest, file_id: int) -> JsonResponse:
    stored_file = get_object_or_404(
        StoredFile,
        pk=file_id,
        user=request.user,
        deleted_at__isnull=True,
    )
    deleted_at = timezone.now()
    stored_file.advance_status(
        StoredFile.Status.DELETING,
        allow_from=StoredFile.Status.values,
    )
    stored_file.deleted_at = deleted_at
    stored_file.save(update_fields=['deleted_at'])
    logger.info(
        "delete_file: soft-deleted file_id=%s user=%s",
        stored_file.id,
        request.user.id,
    )
    return JsonResponse(
        {
            'status': 'ok',
            'deleted_at': deleted_at.isoformat(),
        }
    )


@login_required
@require_POST
def rename_file(request: HttpRequest, file_id: int) -> JsonResponse:
    stored_file = get_object_or_404(
        StoredFile,
        pk=file_id,
        user=request.user,
        deleted_at__isnull=True,
    )
    new_name = (request.POST.get('new_name') or '').strip()
    if not new_name:
        return JsonResponse({'error': 'A new filename is required.'}, status=400)

    sanitized = get_valid_filename(new_name)
    original = stored_file.original_filename or ''
    orig_root, orig_ext = os.path.splitext(original)
    new_root, new_ext = os.path.splitext(sanitized)
    if (orig_ext or new_ext) and orig_ext.lower() != new_ext.lower():
        return JsonResponse({'error': 'File extension cannot be changed.'}, status=400)
    if not new_root and orig_root:
        return JsonResponse({'error': 'Filename cannot be empty.'}, status=400)

    stored_file.original_filename = sanitized
    stored_file.save(update_fields=['original_filename'])
    logger.info(
        "rename_file: renamed file_id=%s user=%s new_name=%s",
        stored_file.id,
        request.user.id,
        sanitized,
    )
    return JsonResponse(
        {
            'status': 'ok',
            'new_name': sanitized,
        }
    )


@login_required
@require_POST
def purge_file(request: HttpRequest, file_id: int) -> JsonResponse:
    stored_file = get_object_or_404(
        StoredFile,
        pk=file_id,
        user=request.user,
        deleted_at__isnull=False,
    )
    bucket_name = stored_file.bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)
    prefixes = set()
    if stored_file.file_uuid:
        base_prefix = (stored_file.per_upload_prefix or '').strip('/')
        if base_prefix:
            prefixes.add(f"{base_prefix}/")
    if stored_file.folder and not stored_file.file_uuid:
        prefixes.add(stored_file.folder)

    removed_objects = 0
    if bucket_name and prefixes:
        for prefix in prefixes:
            try:
                removed_objects += storage_util.delete_prefix(
                    prefix,
                    bucket_name=bucket_name,
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception(
                    "purge_file: failed to delete prefix file_id=%s prefix=%s error=%s",
                    stored_file.id,
                    prefix,
                    exc,
                )
    elif not bucket_name:
        logger.warning(
            "purge_file: bucket missing for file_id=%s, skipping remote cleanup",
            stored_file.id,
        )

    stored_file.delete()
    logger.info(
        "purge_file: permanently deleted file_id=%s user=%s prefixes_deleted=%s",
        file_id,
        request.user.id,
        removed_objects,
    )
    return JsonResponse({'status': 'ok', 'removed_objects': removed_objects})


def _pick_download_filename(stored_file: StoredFile) -> str:
    """Choose a reasonable filename for download headers."""
    candidates = [
        stored_file.original_filename,
        stored_file.folder.rsplit('/', 1)[-1] if stored_file.folder else '',
        str(stored_file.file_uuid) if stored_file.file_uuid else '',
        f'file-{stored_file.pk}',
    ]
    for candidate in candidates:
        candidate = (candidate or '').strip()
        if candidate:
            # Avoid quotes/newlines in Content-Disposition
            return candidate.replace('"', '').replace('\r', '').replace('\n', '')
    return 'download'


@login_required
def download_file(request: HttpRequest, file_id: int) -> HttpResponse:
    stored_file = get_object_or_404(
        StoredFile,
        pk=file_id,
        user=request.user,
        deleted_at__isnull=True,
    )
    if not stored_file.folder:
        logger.warning("download_file: missing object_name for file_id=%s", stored_file.id)
        return HttpResponse('File is not available for download.', status=404)

    bucket_name = stored_file.bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)
    if not bucket_name:
        logger.error("download_file: bucket missing for file_id=%s", stored_file.id)
        return HttpResponse('Storage bucket is not configured.', status=500)

    filename = _pick_download_filename(stored_file)
    disposition = f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quote(filename, safe="")}'

    try:
        signed_url = storage_util.generate_signed_url(
            stored_file.folder,
            bucket_name=bucket_name,
            response_disposition=disposition,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception(
            "download_file: failed to build signed url file_id=%s: %s",
            stored_file.id,
            exc,
        )
        return HttpResponse('Unable to generate download link at the moment.', status=500)

    logger.info(
        "download_file: redirecting to signed url file_id=%s bucket=%s object=%s",
        stored_file.id,
        bucket_name,
        stored_file.folder,
    )
    return redirect(signed_url)


@login_required
def play_video(request: HttpRequest, file_id: int) -> HttpResponse:
    stored_file = get_object_or_404(
        StoredFile,
        pk=file_id,
        user=request.user,
        deleted_at__isnull=True,
    )
    if not stored_file.file_uuid:
        logger.warning(
            "play_video: requested file is not a video file_id=%s",
            stored_file.id,
        )
        return HttpResponse('Video playback is not available for this file.', status=404)

    bucket_name = stored_file.bucket_name or getattr(settings, 'GCS_BUCKET_NAME', None)
    if not bucket_name:
        logger.error("play_video: bucket missing for file_id=%s", stored_file.id)
        return HttpResponse('Storage bucket is not configured.', status=500)

    base_prefix = (stored_file.per_upload_prefix or '').strip('/')
    if not base_prefix:
        logger.warning("play_video: missing per-upload prefix file_id=%s", stored_file.id)
        return HttpResponse('Segmented assets not available for this video.', status=404)

    segmented_prefix = f"{base_prefix}/segmented"
    manifest_object = f"{segmented_prefix}/output.m3u8"
    try:
        manifest_content = storage_util.download_file_as_string(
            manifest_object,
            bucket_name=bucket_name,
        )
    except Exception as exc:
        logger.exception(
            "play_video: failed to download manifest file_id=%s manifest=%s error=%s",
            stored_file.id,
            manifest_object,
            exc,
        )
        return HttpResponse('Unable to fetch segmented manifest at the moment.', status=500)

    if not manifest_content:
        logger.warning(
            "play_video: manifest missing or empty file_id=%s manifest=%s",
            stored_file.id,
            manifest_object,
        )
        return HttpResponse('Segmented manifest not available for this video.', status=404)

    lines = manifest_content.splitlines()
    had_trailing_newline = manifest_content.endswith(('\n', '\r'))

    signed_lines = []
    replacement_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '://' in stripped:
            signed_lines.append(line)
            continue
        segment_object = '/'.join(
            part.strip('/') for part in (segmented_prefix, stripped) if part
        )
        try:
            signed_url = storage_util.generate_signed_url(
                segment_object,
                bucket_name=bucket_name,
            )
        except Exception as exc:
            logger.exception(
                "play_video: failed to sign segment file_id=%s object=%s error=%s",
                stored_file.id,
                segment_object,
                exc,
            )
            return HttpResponse('Unable to prepare playable manifest.', status=500)
        signed_lines.append(signed_url)
        replacement_count += 1

    signed_manifest = "\n".join(signed_lines)
    if had_trailing_newline:
        signed_manifest += "\n"

    padded_user_id = f"{stored_file.user_id:05d}"
    download_name = f"user_{padded_user_id}_{stored_file.file_uuid}_segmented_output.m3u8"

    logger.info(
        "play_video: prepared signed manifest file_id=%s manifest=%s segments_signed=%s",
        stored_file.id,
        manifest_object,
        replacement_count,
    )

    response = HttpResponse(signed_manifest, content_type='application/vnd.apple.mpegurl')
    response['Content-Disposition'] = f'inline; filename="{download_name}"'
    return response


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
