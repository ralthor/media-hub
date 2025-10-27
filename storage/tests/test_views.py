from django.test import TestCase, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch


class UploadViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_get_upload_page(self):
        resp = self.client.get('/upload/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Upload a file', resp.content)

    def test_post_without_file_returns_400(self):
        resp = self.client.post('/upload/', {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b'No file provided', resp.content)

    @patch('storage.processes.upload_to_gcs_and_sign')
    def test_post_with_file_uploads_and_returns_success(self, mock_process):
        mock_process.return_value = {
            'bucket': 'bucket-1',
            'object_name': 'hello.txt',
            'gs_uri': 'gs://bucket-1/hello.txt',
            'signed_url': 'https://signed/url',
        }

        uploaded = SimpleUploadedFile('hello.txt', b'hello world', content_type='text/plain')
        resp = self.client.post('/upload/', {'file': uploaded})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Uploaded to ', resp.content)
        self.assertIn(b'hello.txt', resp.content)
        self.assertIn(b'Temporary access link', resp.content)


class VideoViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    @patch('storage.storage_util.download_file_as_string')
    def test_video_page_renders_manifest_and_logs(self, mock_download):
        mock_manifest = '#EXTM3U\n#EXT-X-ENDLIST\n'
        mock_download.return_value = mock_manifest

        with self.assertLogs('storage.views', level='INFO') as cm:
            resp = self.client.get('/video/')

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        # The manifest content is embedded into JS string; ensure marker text is present
        self.assertIn('#EXTM3U', body)
        # Ensure our logging ran
        combined_logs = '\n'.join(cm.output)
        self.assertIn('video_page: fetching manifest', combined_logs)
        self.assertIn('video_page: fetched manifest', combined_logs)
        self.assertIn('video_page: rendering template', combined_logs)
