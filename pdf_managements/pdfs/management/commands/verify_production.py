import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Fail if required production security and infrastructure settings are unsafe."

    def handle(self, *args, **options):
        failures = []
        engine = settings.DATABASES["default"].get("ENGINE", "")
        secret = settings.SECRET_KEY
        email_backend = settings.EMAIL_BACKEND

        if settings.DEBUG:
            failures.append("DJANGO_DEBUG must be false.")
        if len(secret) < 50 or "insecure" in secret.lower():
            failures.append("DJANGO_SECRET_KEY must be a strong random value of at least 50 characters.")
        if "postgresql" not in engine:
            failures.append("DATABASE_URL must use PostgreSQL, not SQLite.")
        if not settings.ALLOWED_HOSTS or "*" in settings.ALLOWED_HOSTS:
            failures.append("DJANGO_ALLOWED_HOSTS must contain explicit production hosts.")
        if not getattr(settings, "SECURE_SSL_REDIRECT", False):
            failures.append("DJANGO_SECURE_SSL_REDIRECT must be enabled.")
        if not getattr(settings, "SESSION_COOKIE_SECURE", False):
            failures.append("SESSION_COOKIE_SECURE must be enabled.")
        if not getattr(settings, "CSRF_COOKIE_SECURE", False):
            failures.append("CSRF_COOKIE_SECURE must be enabled.")
        if "console" in email_backend or "dummy" in email_backend:
            failures.append("Configure a real SMTP email backend for alerts and system mail.")
        if not settings.REDIS_URL:
            failures.append("REDIS_URL is required for shared production rate-limit/cache state.")
        if settings.SERVE_MEDIA:
            failures.append("DJANGO_SERVE_MEDIA must be false; uploaded media must be served by a protected edge/storage layer.")
        media_root = os.getenv("DJANGO_MEDIA_ROOT", "")
        if not media_root or not Path(media_root).is_absolute():
            failures.append("DJANGO_MEDIA_ROOT must be an absolute path on a persistent encrypted volume.")

        if failures:
            formatted = "\n".join(f"  {index}. {message}" for index, message in enumerate(failures, 1))
            raise CommandError(f"Production verification failed:\n{formatted}")
        self.stdout.write(self.style.SUCCESS("Production configuration verification passed."))
