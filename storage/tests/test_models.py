from django.test import TestCase
from django.contrib.auth import get_user_model

from storage.models import Video


class VideoModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email='video-owner@example.com',
            password='test-pass-12345',
        )

    def test_video_requires_user(self):
        video = Video.objects.create(
            user=self.user,
            bucket_name='bucket-1',
            folder='folder',
            size_bytes=1024,
            original_filename='foo.mp4',
            content_type='video/mp4',
        )
        self.assertEqual(video.user, self.user)
        self.assertIn('bucket-1', str(video))
        self.assertIn('folder', str(video))

