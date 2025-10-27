import os
import tempfile
import logging
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.text import get_valid_filename
from django.conf import settings

from . import processes
from . import storage_util

logger = logging.getLogger(__name__)


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
                object_name=filename,
                expires_seconds=24 * 60 * 60,
            )
        except Exception as e:
            logger.exception("upload_file: GCS upload failed: %s", e)
            context['error'] = f'Failed to upload to storage: {e}'
            return render(request, 'upload.html', context, status=500)

        context['success'] = f'Uploaded to {dest_path}'
        gs_uri = res.get('gs_uri') or f"gs://{res.get('bucket')}/{res.get('object_name')}"
        logger.info("upload_file: GCS upload complete gs_uri=%s", gs_uri)
        context['signed_url'] = res.get('signed_url')
        context['object_name'] = res.get('object_name')
        context['bucket'] = res.get('bucket')
        context['gcs_uri'] = gs_uri
        logger.info("upload_file: returning success response")
        return render(request, 'upload.html', context)

    # GET -> render simple upload form
    logger.info("upload_file: rendering upload form (GET)")
    return render(request, 'upload.html')
