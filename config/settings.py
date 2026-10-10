import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY.")
DEBUG = False
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8092").rstrip("/")
CSRF_TRUSTED_ORIGINS = [PUBLIC_URL]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "lodge",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "lodge.middleware.AuthRateLimitMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "lodge"),
        "USER": os.environ.get("DB_USER", "lodge"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "db"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}
AUTH_USER_MODEL = "lodge.User"
AUTHENTICATION_BACKENDS = ["lodge.auth.MemberBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
TIME_ZONE = os.environ.get("SITE_TIME_ZONE", "America/Chicago")
USE_TZ = True
LANGUAGE_CODE = "en-us"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = os.environ.get("SECURE_COOKIES", "false").lower() == "true"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
if os.environ.get("TRUST_PROXY", "false").lower() == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
REGISTRATION_ENABLED = os.environ.get("REGISTRATION_ENABLED", "true").lower() == "true"
INVITATION_REQUIRED = os.environ.get("INVITATION_REQUIRED", "false").lower() == "true"
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.filebased.EmailBackend")
EMAIL_FILE_PATH = MEDIA_ROOT / "mail"
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "false").lower() == "true"
SMTP_ENCRYPTION_KEY = os.environ.get("SMTP_ENCRYPTION_KEY", "")
SMTP_ENCRYPTION_PREVIOUS_KEYS = [
    key.strip()
    for key in os.environ.get("SMTP_ENCRYPTION_PREVIOUS_KEYS", "").split(",")
    if key.strip()
]
EMAIL_TIMEOUT = 20
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Stellar Skies Lodge <lodge@example.com>")
DATA_UPLOAD_MAX_MEMORY_SIZE = 55 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_tokens": {"()": "lodge.logging.RedactTokenPaths"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "filters": ["redact_tokens"]}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
