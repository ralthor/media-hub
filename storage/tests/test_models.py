from pathlib import Path

from django.test import TestCase
from django.contrib.auth import get_user_model

from storage.models import StoredFile


class StoredFileModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email='video-owner@example.com',
            password='test-pass-12345',
        )

    def test_stored_file_requires_user(self):
        stored = StoredFile.objects.create(
            user=self.user,
            bucket_name='bucket-1',
            folder='folder',
            size_bytes=1024,
            original_filename='foo.mp4',
            content_type='video/mp4',
        )
        self.assertEqual(stored.user, self.user)
        self.assertIn('bucket-1', str(stored))
        self.assertIn('folder', str(stored))

    def test_local_workdir_and_prefix_helpers(self):
        stored = StoredFile.objects.create(
            user=self.user,
            file_uuid='12345678-1234-5678-1234-567812345678',
            bucket_name='bucket-2',
            folder='user/00042/12345678-1234-5678-1234-567812345678/file',
            size_bytes=2048,
            original_filename='clip.mp4',
            content_type='video/mp4',
        )
        self.assertTrue(stored.per_upload_prefix.endswith(str(stored.file_uuid)))
        workdir = Path(stored.local_workdir)
        self.assertEqual(workdir.name, str(stored.file_uuid))
        self.assertEqual(workdir.parent.name, 'uploads')
        self.assertEqual(workdir.parent.parent.name, 'temp')

    def test_status_advance_guards_regression(self):
        stored = StoredFile.objects.create(
            user=self.user,
            bucket_name='bucket-3',
            folder='files/foo.txt',
            size_bytes=512,
            original_filename='foo.txt',
            content_type='text/plain',
        )
        self.assertEqual(stored.status, StoredFile.Status.UPLOADING)
        stored.advance_status(StoredFile.Status.UPLOAD_COMPLETE)
        self.assertEqual(stored.status, StoredFile.Status.UPLOAD_COMPLETE)
        stored.advance_status(StoredFile.Status.PROCESSING)
        self.assertEqual(stored.status, StoredFile.Status.PROCESSING)
        # Regression to uploading should be ignored
        stored.advance_status(StoredFile.Status.UPLOADING)
        self.assertEqual(stored.status, StoredFile.Status.PROCESSING)
