import base64

from .settings import *

# Public fixture key, used only by isolated tests. Never use this in deployment.
SMTP_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()
SMTP_ENCRYPTION_PREVIOUS_KEYS = []

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
