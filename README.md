To run the server

```shell
docker compose up
```

To create django admin password locally:

```shell
docker compose run --rm -e DJANGO_SUPERUSER_USERNAME=ADMIN_USER_NAME -e DJANGO_SUPERUSER_EMAIL=ADMIN_EMAIL@EXAMPLE.COM -e DJANGO_SUPERUSER_PASSWORD=YOUR_CHOICE_OF_PASSWORD web python manage.py createsuperuser --noinput
```
