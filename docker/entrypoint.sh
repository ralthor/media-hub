#!/bin/sh
set -e

# Apply migrations
python manage.py migrate --noinput

# Run development server (auto-reload)
exec python manage.py runserver 0.0.0.0:8000

