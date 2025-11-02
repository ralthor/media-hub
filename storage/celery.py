import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "storage.settings")

# Create Celery application configured via Django's settings module.
app = Celery("storage")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
