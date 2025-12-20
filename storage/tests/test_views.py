import uuid
from pathlib import Path
from unittest.mock import ANY, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from storage.models import StoredFile


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    VIDEO_PROCESSING_ENABLED=False,
)
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

    @patch('storage.upload_helpers.storage_util.upload_local_file')
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
        self.assertEqual(stored.status, StoredFile.Status.READY)
        upload_path = Path(mock_upload.call_args[0][0])
        self.assertEqual(upload_path.parent, Path(stored.local_workdir))

    @patch('storage.upload_helpers.storage_util.upload_local_file')
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
        self.assertEqual(stored.status, StoredFile.Status.READY)
        upload_path = Path(mock_upload.call_args[0][0])
        self.assertEqual(upload_path.parent, Path(stored.local_workdir))

    @patch('storage.upload_helpers.storage_util.upload_local_file')
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
        self.assertEqual(stored.status, StoredFile.Status.READY)
        upload_path = Path(mock_upload.call_args[0][0])
        self.assertEqual(upload_path.parent, Path(stored.local_workdir))

    @patch('storage.upload_helpers._extract_video_duration_seconds', return_value=125)
    @patch('storage.upload_helpers.storage_util.upload_local_file')
    @patch('storage.upload_helpers.uuid.uuid4')
    def test_video_upload_records_duration_seconds(self, mock_uuid, mock_upload, mock_extract):
        fake_uuid = uuid.UUID('0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f0f0f')
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
        stored = StoredFile.objects.get()
        self.assertEqual(stored.duration_seconds, 125)
        mock_extract.assert_called_once()

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
        resp = self.client.get('/')
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
            size_bytes=1572864,
            original_filename='sample.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
            duration_seconds=125,
        )
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('sample.mp4', body)
        self.assertIn('videos-bucket', body)
        self.assertIn('video', body)
        self.assertIn('READY', body)
        self.assertIn('1.5 MB', body)
        self.assertIn('2:05', body)

    def test_dashboard_sort_by_name_ascending(self):
        self.client.login(username=self.user.email, password=self.password)
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/b-name.txt',
            size_bytes=128,
            original_filename='zulu.txt',
            content_type='text/plain',
            status=StoredFile.Status.READY,
        )
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/a-name.txt',
            size_bytes=128,
            original_filename='alpha.txt',
            content_type='text/plain',
            status=StoredFile.Status.READY,
        )
        resp = self.client.get('/?sort=name&direction=asc')
        self.assertEqual(resp.status_code, 200)
        ordered_names = [entry['original_filename'] for entry in resp.context['uploaded_files']]
        self.assertEqual(ordered_names, ['alpha.txt', 'zulu.txt'])
        self.assertEqual(resp.context['sorting']['current'], 'name')
        self.assertEqual(resp.context['sorting']['direction'], 'asc')

    def test_dashboard_omits_deleted_files(self):
        self.client.login(username=self.user.email, password=self.password)
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/archive.zip',
            size_bytes=100,
            original_filename='archive.zip',
            content_type='application/zip',
            status=StoredFile.Status.READY,
            deleted_at=timezone.now(),
        )
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('archive.zip', resp.content.decode())

    def test_delete_file_marks_record_deleted(self):
        self.client.login(username=self.user.email, password=self.password)
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/report.pdf',
            size_bytes=256,
            original_filename='report.pdf',
            content_type='application/pdf',
            status=StoredFile.Status.READY,
        )
        resp = self.client.post(f'/files/{stored.id}/delete/')
        self.assertEqual(resp.status_code, 200)
        stored.refresh_from_db()
        self.assertIsNotNone(stored.deleted_at)

    def test_rename_file_prevents_extension_change(self):
        self.client.login(username=self.user.email, password=self.password)
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/report.pdf',
            size_bytes=256,
            original_filename='report.pdf',
            content_type='application/pdf',
            status=StoredFile.Status.READY,
        )
        resp = self.client.post(
            f'/files/{stored.id}/rename/',
            data={'new_name': 'report.txt'},
        )
        self.assertEqual(resp.status_code, 400)
        stored.refresh_from_db()
        self.assertEqual(stored.original_filename, 'report.pdf')

        resp_ok = self.client.post(
            f'/files/{stored.id}/rename/',
            data={'new_name': 'quarterly_report.pdf'},
        )
        self.assertEqual(resp_ok.status_code, 200)
        stored.refresh_from_db()
        self.assertEqual(stored.original_filename, 'quarterly_report.pdf')

    def test_bin_page_lists_deleted_files(self):
        self.client.login(username=self.user.email, password=self.password)
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/old.txt',
            size_bytes=10,
            original_filename='old.txt',
            content_type='text/plain',
            status=StoredFile.Status.DELETING,
            deleted_at=timezone.now(),
        )
        resp = self.client.get('/bin/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('old.txt', body)
        self.assertIn('Delete Permanently', body)

    def test_bin_page_sort_by_category(self):
        self.client.login(username=self.user.email, password=self.password)
        now = timezone.now()
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/doc.txt',
            size_bytes=10,
            original_filename='doc.txt',
            content_type='application/octet-stream',
            status=StoredFile.Status.DELETING,
            deleted_at=now,
        )
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='photos-bucket',
            folder=f'user/{self.user.id:05d}/files/photo.jpg',
            size_bytes=10,
            original_filename='photo.jpg',
            content_type='image/jpeg',
            status=StoredFile.Status.DELETING,
            deleted_at=now,
        )
        StoredFile.objects.create(
            user=self.user,
            file_uuid=uuid.UUID('12345678-1234-5678-1234-567812345678'),
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/files/video.mp4',
            size_bytes=10,
            original_filename='video.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.DELETING,
            deleted_at=now,
        )
        resp = self.client.get('/bin/?sort=category&direction=asc')
        self.assertEqual(resp.status_code, 200)
        ordered_categories = [
            entry['category'] for entry in resp.context['deleted_files']
        ]
        self.assertEqual(ordered_categories, ['file', 'photo', 'video'])

    @patch('storage.views.storage_util.delete_prefix', return_value=3)
    def test_purge_file_deletes_record_and_storage(self, mock_delete_prefix):
        self.client.login(username=self.user.email, password=self.password)
        file_uuid = uuid.UUID('99999999-8888-7777-6666-555555555555')
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=file_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{file_uuid}/file',
            size_bytes=10,
            original_filename='clip.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.DELETING,
            deleted_at=timezone.now(),
        )
        resp = self.client.post(f'/bin/files/{stored.id}/purge/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(StoredFile.objects.filter(id=stored.id).exists())
        mock_delete_prefix.assert_called_once()

    def test_restore_file_clears_deleted_at(self):
        self.client.login(username=self.user.email, password=self.password)
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/report.pdf',
            size_bytes=256,
            original_filename='report.pdf',
            content_type='application/pdf',
            status=StoredFile.Status.DELETING,
            deleted_at=timezone.now(),
        )
        resp = self.client.post(f'/bin/files/{stored.id}/restore/')
        self.assertEqual(resp.status_code, 200)
        stored.refresh_from_db()
        self.assertIsNone(stored.deleted_at)
        self.assertEqual(stored.status, StoredFile.Status.READY)

    def test_video_library_lists_only_videos(self):
        self.client.login(username=self.user.email, password=self.password)
        video_uuid = uuid.UUID('11111111-2222-3333-4444-555555555555')
        StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{video_uuid}/file',
            size_bytes=2048,
            original_filename='sample.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
            duration_seconds=3723,
        )
        StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/doc.pdf',
            size_bytes=1024,
            original_filename='doc.pdf',
            content_type='application/pdf',
            status=StoredFile.Status.READY,
        )

        resp = self.client.get('/videos/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('sample.mp4', body)
        self.assertNotIn('doc.pdf', body)
        self.assertIn('2 KB', body)
        self.assertIn('1:02:03', body)

    def test_video_library_sort_by_bucket_descending(self):
        self.client.login(username=self.user.email, password=self.password)
        later_uuid = uuid.UUID('aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb')
        earlier_uuid = uuid.UUID('dddddddd-1111-2222-3333-cccccccccccc')
        StoredFile.objects.create(
            user=self.user,
            file_uuid=earlier_uuid,
            bucket_name='alpha-bucket',
            folder=f'user/{self.user.id:05d}/{earlier_uuid}/file',
            size_bytes=1024,
            original_filename='alpha.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
        )
        StoredFile.objects.create(
            user=self.user,
            file_uuid=later_uuid,
            bucket_name='zulu-bucket',
            folder=f'user/{self.user.id:05d}/{later_uuid}/file',
            size_bytes=1024,
            original_filename='zulu.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.PROCESSING,
        )
        resp = self.client.get('/videos/?sort=bucket&direction=desc')
        self.assertEqual(resp.status_code, 200)
        ordered_buckets = [entry['bucket'] for entry in resp.context['videos']]
        self.assertEqual(ordered_buckets, ['zulu-bucket', 'alpha-bucket'])

    def test_video_library_ignores_deleted_files(self):
        self.client.login(username=self.user.email, password=self.password)
        video_uuid = uuid.UUID('00000000-1111-2222-3333-444444444444')
        StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{video_uuid}/file',
            size_bytes=2048,
            original_filename='gone.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
            deleted_at=timezone.now(),
        )
        resp = self.client.get('/videos/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('gone.mp4', resp.content.decode())

    @patch('storage.views.storage_util.download_file_as_string')
    def test_play_video_rewrites_manifest_with_streaming_urls(self, mock_download):
        self.client.login(username=self.user.email, password=self.password)
        video_uuid = uuid.UUID('99999999-8888-7777-6666-555555555555')
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{video_uuid}/file',
            size_bytes=1024,
            original_filename='clip.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
        )
        manifest = '#EXTM3U\n#EXTINF:10,\nsegment_00001.ts\nhttps://cdn.example/segment.m4s\n'
        mock_download.return_value = manifest

        resp = self.client.get(f'/files/{stored.id}/play/')

        self.assertEqual(resp.status_code, 200)
        self.assertIn('application/vnd.apple.mpegurl', resp['Content-Type'])
        disposition = resp['Content-Disposition']
        self.assertIn('user_{:05d}_{}_segmented_output.m3u8'.format(self.user.id, video_uuid), disposition)
        body = resp.content.decode()
        expected_segment = f'/videos/{video_uuid}/segments/segment_00001.ts'
        self.assertIn(expected_segment, body)
        self.assertIn('https://cdn.example/segment.m4s', body)
        mock_download.assert_called_once_with(
            f'user/{self.user.id:05d}/{video_uuid}/segmented/output.m3u8',
            bucket_name='videos-bucket',
        )
        # Ensure absolute URLs are preserved
        self.assertIn('#EXTM3U', body)


class SegmentStreamTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'test-pass-stream'
        self.user = get_user_model().objects.create_user(
            email='streamer@example.com',
            password=self.password,
        )
        self.client.login(username=self.user.email, password=self.password)

    @patch('storage.views.storage_util.generate_signed_url')
    def test_stream_segment_redirects_to_signed_url(self, mock_generate):
        video_uuid = uuid.UUID('12121212-3434-5656-7878-909090909090')
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{video_uuid}/file',
            size_bytes=2048,
            original_filename='movie.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
        )
        mock_generate.return_value = 'https://signed.example/segment_00001.ts'

        resp = self.client.get(f'/videos/{video_uuid}/segments/segment_00001.ts')

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], 'https://signed.example/segment_00001.ts')
        self.assertEqual(resp['Cache-Control'], 'no-store')
        expected_object = (
            f'user/{self.user.id:05d}/{video_uuid}/segmented/segment_00001.ts'
        )
        mock_generate.assert_called_once_with(
            expected_object,
            bucket_name=stored.bucket_name,
        )

    @patch('storage.views.storage_util.generate_signed_url')
    def test_stream_segment_handles_missing_prefix(self, mock_generate):
        video_uuid = uuid.UUID('aaaaaaaa-0000-1111-2222-bbbbbbbbbbbb')
        StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder='',
            size_bytes=1024,
            original_filename='broken.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
        )

        resp = self.client.get(f'/videos/{video_uuid}/segments/segment_00001.ts')

        self.assertEqual(resp.status_code, 404)
        mock_generate.assert_not_called()


class FileAccessControlTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'test-pass-access'
        self.user = get_user_model().objects.create_user(
            email='owner@example.com',
            password=self.password,
        )
        self.other_user = get_user_model().objects.create_user(
            email='other@example.com',
            password='other-pass-123',
        )
        self.client.login(username=self.user.email, password=self.password)

    def test_download_file_denies_other_users_asset(self):
        stored = StoredFile.objects.create(
            user=self.other_user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.other_user.id:05d}/files/private.txt',
            size_bytes=64,
            original_filename='private.txt',
            content_type='text/plain',
            status=StoredFile.Status.READY,
        )
        with patch('storage.views.storage_util.generate_signed_url') as mock_generate:
            resp = self.client.get(f'/files/{stored.id}/download/')

        self.assertEqual(resp.status_code, 404)
        mock_generate.assert_not_called()

    def test_play_video_denies_other_users_asset(self):
        video_uuid = uuid.UUID('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        stored = StoredFile.objects.create(
            user=self.other_user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.other_user.id:05d}/{video_uuid}/file',
            size_bytes=2048,
            original_filename='secret.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.READY,
        )
        with patch('storage.views.storage_util.download_file_as_string') as mock_download, patch(
            'storage.views.storage_util.generate_signed_url'
        ) as mock_generate:
            resp = self.client.get(f'/files/{stored.id}/play/')

        self.assertEqual(resp.status_code, 404)
        mock_download.assert_not_called()
        mock_generate.assert_not_called()

    def test_delete_file_denies_other_users_asset(self):
        stored = StoredFile.objects.create(
            user=self.other_user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.other_user.id:05d}/files/private.txt',
            size_bytes=64,
            original_filename='private.txt',
            content_type='text/plain',
            status=StoredFile.Status.READY,
        )
        resp = self.client.post(f'/files/{stored.id}/delete/')
        self.assertEqual(resp.status_code, 404)
        stored.refresh_from_db()
        self.assertIsNone(stored.deleted_at)

    def test_download_denies_deleted_file(self):
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=None,
            bucket_name='docs-bucket',
            folder=f'user/{self.user.id:05d}/files/private.txt',
            size_bytes=64,
            original_filename='private.txt',
            content_type='text/plain',
            status=StoredFile.Status.DELETING,
            deleted_at=timezone.now(),
        )
        with patch('storage.views.storage_util.generate_signed_url') as mock_generate:
            resp = self.client.get(f'/files/{stored.id}/download/')
        self.assertEqual(resp.status_code, 404)
        mock_generate.assert_not_called()

    def test_play_video_denies_deleted_file(self):
        video_uuid = uuid.UUID('12345678-1234-5678-1234-567812345678')
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid=video_uuid,
            bucket_name='videos-bucket',
            folder=f'user/{self.user.id:05d}/{video_uuid}/file',
            size_bytes=2048,
            original_filename='secret.mp4',
            content_type='video/mp4',
            status=StoredFile.Status.DELETING,
            deleted_at=timezone.now(),
        )
        with patch('storage.views.storage_util.download_file_as_string') as mock_download, patch(
            'storage.views.storage_util.generate_signed_url'
        ) as mock_generate:
            resp = self.client.get(f'/files/{stored.id}/play/')
        self.assertEqual(resp.status_code, 404)
        mock_download.assert_not_called()
        mock_generate.assert_not_called()
