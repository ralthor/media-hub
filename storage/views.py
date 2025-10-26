import os
import tempfile
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.text import get_valid_filename
from django.conf import settings

from . import processes


def upload_file(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        uploaded = request.FILES.get('file')
        context = {}
        if not uploaded:
            context['error'] = 'No file provided.'
            return render(request, 'upload.html', context, status=400)

        filename = get_valid_filename(os.path.basename(uploaded.name))

        # First write to local filesystem (MEDIA_ROOT or temp dir)
        dest_dir = getattr(settings, 'MEDIA_ROOT', None) or tempfile.gettempdir()
        dest_dir = str(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)

        try:
            with open(dest_path, 'wb') as out:
                for chunk in uploaded.chunks():
                    out.write(chunk)
        except OSError as e:
            context['error'] = f'Failed to write file: {e}'
            return render(request, 'upload.html', context, status=500)

        # Then upload the file to GCS and generate a signed URL
        try:
            res = processes.upload_to_gcs_and_sign(
                uploaded,
                object_name=filename,
                expires_seconds=24 * 60 * 60,
            )
        except Exception as e:
            context['error'] = f'Failed to upload to storage: {e}'
            return render(request, 'upload.html', context, status=500)

        context['success'] = f'Uploaded to {dest_path}'
        gs_uri = res.get('gs_uri') or f"gs://{res.get('bucket')}/{res.get('object_name')}"
        context['signed_url'] = res.get('signed_url')
        context['object_name'] = res.get('object_name')
        context['bucket'] = res.get('bucket')
        context['gcs_uri'] = gs_uri
        return render(request, 'upload.html', context)

    # GET -> render simple upload form
    return render(request, 'upload.html')
