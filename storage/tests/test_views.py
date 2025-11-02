import uuid
from unittest.mock import ANY, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from storage.models import StoredFile


class UploadViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'test-pass-12345'
        self.user = get_user_model().objects.create_user(
            email='uploader@example.com',
            password=self.password,
        )
        self.client.login(username=self.user.email, password=self.password)

    def test_get_upload_page(self):
        resp = self.client.get('/upload/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Upload a file', resp.content)

    def test_post_without_file_returns_400(self):
        with self.assertLogs('storage.views', level='WARNING') as cm:
            resp = self.client.post('/upload/', {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b'No file provided', resp.content)
        combined_logs = '\n'.join(cm.output)
        self.assertIn('upload_file: no file provided in POST', combined_logs)

    def test_upload_redirects_to_login_if_anonymous(self):
        self.client.logout()
        resp = self.client.get('/upload/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('/accounts/login/'))

    @patch('storage.views.storage_util.upload_local_file')
    def test_post_with_file_uploads_and_returns_success(self, mock_upload):
        expected_object = f"user/{self.user.id:05d}/files/hello.txt"
        mock_upload.return_value = {
            'bucket': 'bucket-1',
            'object_name': expected_object,
            'gs_uri': f'gs://bucket-1/{expected_object}',
        }

        uploaded = SimpleUploadedFile('hello.txt', b'hello world', content_type='text/plain')
        resp = self.client.post('/upload/', {'file': uploaded})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Uploaded to ', resp.content)
        self.assertIn(b'hello.txt', resp.content)
        mock_upload.assert_called_once_with(
            ANY,
            destination=expected_object,
            bucket_name=None,
            content_type='text/plain',
        )
        stored = StoredFile.objects.get()
        self.assertIsNone(stored.file_uuid)
        self.assertEqual(stored.bucket_name, 'bucket-1')
        self.assertEqual(stored.folder, expected_object)
        self.assertEqual(stored.content_type, 'text/plain')

    @patch('storage.views.storage_util.upload_local_file')
    @patch('storage.upload_helpers.uuid.uuid4')
    def test_video_upload_creates_video_entry(self, mock_uuid, mock_upload):
        fake_uuid = uuid.UUID('12345678-1234-5678-1234-567812345678')
        mock_uuid.return_value = fake_uuid
        expected_object = f"user/{self.user.id:05d}/{fake_uuid}/file"
        mock_upload.return_value = {
            'bucket': 'videos-bucket',
            'object_name': expected_object,
            'gs_uri': f'gs://videos-bucket/{expected_object}',
        }

        uploaded = SimpleUploadedFile('clip.mp4', b'video-bytes', content_type='video/mp4')
        resp = self.client.post('/upload/', {'file': uploaded})
        self.assertEqual(resp.status_code, 200)
        mock_upload.assert_called_once_with(
            ANY,
            destination=expected_object,
            bucket_name=None,
            content_type='video/mp4',
        )
        stored = StoredFile.objects.get()
        self.assertEqual(stored.file_uuid, fake_uuid)
        self.assertEqual(stored.folder, expected_object)
        self.assertEqual(stored.bucket_name, 'videos-bucket')
        self.assertEqual(stored.size_bytes, len(b'video-bytes'))
        self.assertEqual(stored.original_filename, 'clip.mp4')
        self.assertEqual(stored.content_type, 'video/mp4')

    @patch('storage.views.storage_util.upload_local_file')
    @patch('storage.upload_helpers.uuid.uuid4')
    def test_video_upload_detected_by_guessed_type(self, mock_uuid, mock_upload):
        fake_uuid = uuid.UUID('fedcba98-7654-3210-fedc-ba9876543210')
        mock_uuid.return_value = fake_uuid
        expected_object = f"user/{self.user.id:05d}/{fake_uuid}/file"
        mock_upload.return_value = {
            'bucket': 'videos-bucket',
            'object_name': expected_object,
            'gs_uri': f'gs://videos-bucket/{expected_object}',
        }

        with patch('storage.upload_helpers.mimetypes.guess_type', return_value=('video/mpeg', None)) as mock_guess:
            uploaded = SimpleUploadedFile(
                'clip.custom',
                b'video-bytes',
                content_type='application/octet-stream',
            )
            resp = self.client.post('/upload/', {'file': uploaded})
        self.assertEqual(resp.status_code, 200)
        mock_upload.assert_called_once_with(
            ANY,
            destination=expected_object,
            bucket_name=None,
            content_type='video/mpeg',
        )
        mock_guess.assert_called()
        stored = StoredFile.objects.get()
        self.assertEqual(stored.file_uuid, fake_uuid)
        self.assertEqual(stored.folder, expected_object)
        self.assertEqual(stored.bucket_name, 'videos-bucket')
        self.assertEqual(stored.size_bytes, len(b'video-bytes'))
        self.assertEqual(stored.original_filename, 'clip.custom')
        self.assertEqual(stored.content_type, 'video/mpeg')

class VideoViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'test-pass-54321'
        self.user = get_user_model().objects.create_user(
            email='viewer@example.com',
            password=self.password,
        )

    @patch('storage.storage_util.download_file_as_string')
    def test_video_page_renders_manifest_and_logs(self, mock_download):
        mock_manifest = '#EXTM3U\n#EXT-X-ENDLIST\n'
        mock_download.return_value = mock_manifest

        self.client.login(username=self.user.email, password=self.password)

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

    def test_video_requires_login(self):
        resp = self.client.get('/video/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('/accounts/login/'))


class DashboardViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'test-pass-67890'
        self.user = get_user_model().objects.create_user(
            email='dashboard@example.com',
            password=self.password,
        )

    def test_dashboard_requires_login(self):
        resp = self.client.get('/dashboard/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('/accounts/login/'))

    def test_dashboard_lists_uploaded_files(self):
        self.client.login(username=self.user.email, password=self.password)
        video_uuid = uuid.UUID('87654321-4321-8765-4321-876543218765')
        StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{video_uuid}/file',
            size_bytes=2048,
            original_filename='sample.mp4',
            content_type='video/mp4',
        )
        resp = self.client.get('/dashboard/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('sample.mp4', body)
        self.assertIn('videos-bucket', body)
        self.assertIn('video', body)
