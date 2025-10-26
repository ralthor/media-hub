To run the server

```shell
docker compose up
```

To create django admin password locally:

```shell
docker compose run --rm -e DJANGO_SUPERUSER_USERNAME=ADMIN_USER_NAME -e DJANGO_SUPERUSER_EMAIL=ADMIN_EMAIL@EXAMPLE.COM -e DJANGO_SUPERUSER_PASSWORD=YOUR_CHOICE_OF_PASSWORD web python manage.py createsuperuser --noinput
```

To run the tests

```shell
python manage.py test -v 2
```

GCS credentials with Docker Compose

- Place your GCS service account JSON at `secrets/gcs-key.json` (not committed).
- Ensure `.env` contains:

```
GOOGLE_APPLICATION_CREDENTIALS=/var/secrets/gcs-key.json
GCS_BUCKET_NAME=your-bucket-name
# GCP_PROJECT=your-project-id   # optional if not inferred
```

- The Compose service mounts the key file read-only and exposes the path the app uses for ADC:

```yaml
services:
  web:
    volumes:
      - ./secrets/gcs-key.json:/var/secrets/gcs-key.json:ro
```

The app will use Application Default Credentials via `google-cloud-storage` when
`GOOGLE_APPLICATION_CREDENTIALS` is set, and read `GCS_BUCKET_NAME` for the target bucket.
