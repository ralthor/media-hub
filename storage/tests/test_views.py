from django.test import TestCase, Client
from django.core.files.uploadedfile import SimpleUploadedFile


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

    def test_post_with_file_writes_and_returns_success(self):
        uploaded = SimpleUploadedFile('hello.txt', b'hello world', content_type='text/plain')
        resp = self.client.post('/upload/', {'file': uploaded})
        self.assertEqual(resp.status_code, 200)
        # Response should mention destination path and filename without hardcoding OS paths
        self.assertIn(b'Uploaded to ', resp.content)
        self.assertIn(b'hello.txt', resp.content)
