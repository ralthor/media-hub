import os
import tempfile
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.text import get_valid_filename
from django.conf import settings


def upload_file(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        uploaded = request.FILES.get('file')
        context = {}
        if not uploaded:
            context['error'] = 'No file provided.'
            return render(request, 'upload.html', context, status=400)

        filename = get_valid_filename(os.path.basename(uploaded.name))

        dest_dir = getattr(settings, 'MEDIA_ROOT', None) or tempfile.gettempdir()
        dest_dir = str(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)

        try:
            with open(dest_path, 'wb') as out:
                for chunk in uploaded.chunks():
                    out.write(chunk)
            context['success'] = f'Uploaded to {dest_path}'
        except OSError as e:
            context['error'] = f'Failed to write file: {e}'
            return render(request, 'upload.html', context, status=500)

        return render(request, 'upload.html', context)

    # GET -> render simple upload form
    return render(request, 'upload.html')
