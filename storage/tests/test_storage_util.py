from unittest import TestCase
from unittest.mock import patch, MagicMock

from django.test import override_settings


class StorageUtilTests(TestCase):
    def test_build_public_url_default(self):
        with override_settings(GCS_BUCKET_NAME='my-bucket'):
            from storage.storage_util import build_public_url

            url = build_public_url('path/to/file.txt')
            self.assertEqual(url, 'https://storage.googleapis.com/my-bucket/path/to/file.txt')

    def test_build_public_url_with_base(self):
        with override_settings(GCS_BUCKET_NAME='my-bucket', GCS_PUBLIC_BASE_URL='https://cdn.example.com'):  # noqa: E501
            from storage.storage_util import build_public_url

            url = build_public_url('/path/to/file.txt')
            self.assertEqual(url, 'https://cdn.example.com/path/to/file.txt')

    @patch('storage.storage_util.service_account.Credentials.from_service_account_file')
    @patch('storage.storage_util.storage.Client')
    def test_get_gcs_client_with_credentials(self, mock_client, mock_creds_from_file):
        with override_settings(GCS_CREDENTIALS_FILE='/tmp/key.json', GCP_PROJECT='proj-1'):
            from storage.storage_util import get_gcs_client

            get_gcs_client()
            mock_creds_from_file.assert_called_once_with('/tmp/key.json')
            mock_client.assert_called_once()
            _, kwargs = mock_client.call_args
            self.assertEqual(kwargs.get('project'), 'proj-1')
            self.assertIn('credentials', kwargs)

    @patch('storage.storage_util.storage.Client')
    def test_get_bucket_uses_configured_name(self, mock_client):
        mock_bucket = MagicMock()
        mock_client.return_value.bucket.return_value = mock_bucket

        with override_settings(GCS_BUCKET_NAME='bucket-123'):
            from storage.storage_util import get_bucket

            bucket = get_bucket()
            self.assertIs(bucket, mock_bucket)
            mock_client.return_value.bucket.assert_called_once_with('bucket-123')

    @patch('storage.storage_util.storage.Client')
    def test_upload_fileobj_calls_blob_upload(self, mock_client):
        mock_blob = MagicMock()
        mock_blob.bucket.name = 'bucket-123'
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client.return_value.bucket.return_value = mock_bucket

        file_content = b'abcdefg'
        with override_settings(GCS_BUCKET_NAME='bucket-123'):
            from storage.storage_util import upload_fileobj
            import io

            res = upload_fileobj(io.BytesIO(file_content), 'folder/thing.txt', make_public=True)

        mock_bucket.blob.assert_called_once_with('folder/thing.txt')
        mock_blob.upload_from_file.assert_called()
        # ensure content type default was inferred
        _, kwargs = mock_blob.upload_from_file.call_args
        self.assertEqual(kwargs.get('content_type'), 'text/plain')
        self.assertTrue(kwargs.get('rewind'))
        mock_blob.make_public.assert_called_once()
        self.assertEqual(res['bucket'], 'bucket-123')
        self.assertEqual(res['object_name'], 'folder/thing.txt')
        self.assertEqual(res['gs_uri'], 'gs://bucket-123/folder/thing.txt')

    @patch('storage.storage_util.storage.Client')
    def test_generate_signed_url(self, mock_client):
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = 'https://signed-url'
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client.return_value.bucket.return_value = mock_bucket

        with override_settings(GCS_BUCKET_NAME='bucket-123', GCS_SIGNED_URL_TTL=120):
            from storage.storage_util import generate_signed_url

            url = generate_signed_url('file.bin', method='GET', content_type=None)

        self.assertEqual(url, 'https://signed-url')
        mock_bucket.blob.assert_called_once_with('file.bin')
        mock_blob.generate_signed_url.assert_called()
        _, kwargs = mock_blob.generate_signed_url.call_args
        self.assertEqual(kwargs.get('version'), 'v4')

    @patch('storage.storage_util.storage.Client')
    def test_object_exists_and_delete(self, mock_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client.return_value.bucket.return_value = mock_bucket

        with override_settings(GCS_BUCKET_NAME='bucket-123'):
            from storage.storage_util import object_exists, delete_object

            self.assertTrue(object_exists('a/b/c.txt'))
            delete_object('a/b/c.txt')

        self.assertEqual(mock_bucket.blob.call_count, 2)
        mock_blob.exists.assert_called_once()
        mock_blob.delete.assert_called_once()

