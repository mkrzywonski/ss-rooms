"""Live email transport settings, encrypted secrets, and administrator forms."""

from email.utils import formataddr, parseaddr

from cryptography.fernet import Fernet, MultiFernet
from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import get_connection
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from .models import EmailConfiguration
from .services import audit, lock_user_admin, queue_email

TEST_SUBJECT = "Lodge email configuration test"


@sensitive_variables("__ALL__")
def cipher():
    # Independent random key, kept in protected deployment configuration, not the DB.
    try:
        return Fernet(settings.SMTP_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        raise ImproperlyConfigured("Set SMTP_ENCRYPTION_KEY to a valid Fernet key.") from None


@sensitive_variables("__ALL__")
def decryption_cipher():
    try:
        return MultiFernet(
            [cipher(), *[Fernet(key.encode()) for key in settings.SMTP_ENCRYPTION_PREVIOUS_KEYS]]
        )
    except (ValueError, TypeError):
        raise ImproperlyConfigured("Invalid previous SMTP encryption key.") from None


@sensitive_variables("__ALL__")
def encrypt_password(value):
    return cipher().encrypt(value.encode()).decode() if value else ""


@sensitive_variables("__ALL__")
def decrypt_password(value):
    return decryption_cipher().decrypt(value.encode()).decode() if value else ""


def current_configuration():
    return EmailConfiguration.objects.filter(pk=1).first() or EmailConfiguration()


class EmailSettingsForm(forms.Form):
    host = forms.RegexField(
        r"^[a-zA-Z0-9.-]+$",
        max_length=253,
        label="SMTP server",
        help_text="Hostname only, for example smtp.example.com. No URL or port.",
    )
    port = forms.IntegerField(min_value=1, max_value=65535, initial=587)
    security = forms.ChoiceField(choices=EmailConfiguration._meta.get_field("security").choices)
    username = forms.CharField(required=False, max_length=254, label="SMTP account / username")
    password = forms.CharField(
        required=False,
        max_length=1000,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        label="SMTP password / app password",
        help_text="Leave blank to keep the saved password. The saved value is never displayed.",
    )
    clear_password = forms.BooleanField(required=False, label="Clear saved SMTP password")
    from_address = forms.EmailField(label="Sender email address")
    from_name = forms.CharField(required=False, max_length=120, label="Sender display name")
    revision = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, config=None, **kwargs):
        self.config = config or current_configuration()
        super().__init__(*args, **kwargs)
        self.initial.update(
            {
                key: getattr(self.config, key)
                for key in (
                    "host",
                    "port",
                    "security",
                    "username",
                    "from_address",
                    "from_name",
                    "revision",
                )
            }
        )
        if not self.config.pk or not self.config.revision:
            name, address = parseaddr(settings.DEFAULT_FROM_EMAIL)
            self.initial.update(
                host=settings.EMAIL_HOST,
                port=settings.EMAIL_PORT,
                security="ssl" if settings.EMAIL_USE_SSL else "starttls",
                username=settings.EMAIL_HOST_USER,
                from_address=address,
                from_name=name,
            )
        self.has_password = bool(self.config.encrypted_password)

    def clean(self):
        data = super().clean()
        if data.get("password") and data.get("clear_password"):
            self.add_error(
                "clear_password", "Enter a replacement password or clear the saved one, not both."
            )
        # A saved password belongs to the previous account; do not silently
        # send it to a new host or username.
        changed_account = (
            data.get("host") != self.config.host or data.get("username") != self.config.username
        )
        if (
            self.has_password
            and changed_account
            and not (data.get("password") or data.get("clear_password"))
        ):
            self.add_error(
                "password",
                "Changing the SMTP server or account requires a new password or explicitly clearing it.",
            )
        if any(c in data.get("from_name", "") for c in "\r\n"):
            self.add_error("from_name", "Sender name cannot contain line breaks.")
        return data


@sensitive_variables("__ALL__")
@transaction.atomic
def save_configuration(actor, data, send_test=False):
    actor = lock_user_admin(actor)
    config = current_configuration()
    form = EmailSettingsForm(data, config=config)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    values = form.cleaned_data
    if values["revision"] != config.revision:
        raise ValidationError("Email settings changed. Reload before saving.")
    for key in ("host", "port", "security", "username", "from_address", "from_name"):
        setattr(config, key, values[key])
    if values["password"]:
        config.encrypted_password = encrypt_password(values["password"])
    elif values["clear_password"]:
        config.encrypted_password = ""
    config.revision += 1
    config.full_clean()
    config.save()
    audit(
        actor,
        "email_configuration_changed",
        config,
        password_changed=bool(values["password"] or values["clear_password"]),
    )
    if send_test:
        queue_email(
            [actor.email],
            TEST_SUBJECT,
            "This is a test message from the Stellar Skies Lodge email configuration page.",
        )
        audit(actor, "email_test_queued", config)
    return config


def transport():
    """Read fresh settings per message, so the running worker needs no restart."""
    config = current_configuration()
    if not config.revision:
        # Bootstrap only, before an administrator has saved SMTP settings.
        return get_connection(), settings.DEFAULT_FROM_EMAIL
    connection = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=config.host,
        port=config.port,
        username=config.username,
        password=decrypt_password(config.encrypted_password),
        use_tls=config.security == "starttls",
        use_ssl=config.security == "ssl",
        timeout=settings.EMAIL_TIMEOUT,
    )
    return connection, formataddr((config.from_name, config.from_address))
