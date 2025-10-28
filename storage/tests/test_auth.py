from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.conf import settings


class CustomUserModelTests(TestCase):
    def test_settings_points_to_custom_user(self):
        self.assertEqual(settings.AUTH_USER_MODEL, 'storage.User')

    def test_user_manager_create_user_and_superuser(self):
        User = get_user_model()

        # create_user
        user = User.objects.create_user(email='user@example.com', password='pass12345')
        self.assertIsNotNone(user.id)
        self.assertEqual(user.email, 'user@example.com')
        self.assertTrue(user.check_password('pass12345'))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

        # create_superuser
        admin = User.objects.create_superuser(email='admin@example.com', password='pass12345')
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)


class AuthViewsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.User = get_user_model()
        self.password = 'test-pass-12345'
        self.user = self.User.objects.create_user(email='login@example.com', password=self.password)

    def test_login_view_renders_and_allows_login_with_email(self):
        # GET should render our login page
        resp = self.client.get('/accounts/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Login')
        self.assertTemplateUsed(resp, 'registration/login.html')

        # POST should authenticate using email in the username field
        resp = self.client.post('/accounts/login/', {
            'username': self.user.email,  # Django uses USERNAME_FIELD internally
            'password': self.password,
        })
        self.assertIn(resp.status_code, (302, 303))

    def test_logout_with_next_redirects_and_logs_out(self):
        # Log in first
        self.client.login(username=self.user.email, password=self.password)

        # Logout and redirect to '/'
        resp = self.client.get('/accounts/logout/?next=/')
        self.assertIn(resp.status_code, (302, 303))
        self.assertTrue(resp['Location'].endswith('/'))

        # Verify user is logged out
        resp = self.client.get('/upload/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('/accounts/login/'))

    def test_password_reset_view_renders(self):
        resp = self.client.get('/accounts/password_reset/')
        self.assertEqual(resp.status_code, 200)
        # Contains a form with an email field
        self.assertContains(resp, 'name="email"')
