import base64
import hashlib
import io
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from . import email_config
from .models import AuditEvent, EmailConfiguration


class SMTPEncryptionTests(TestCase):
    def config(self, token):
        return EmailConfiguration.objects.create(
            host="smtp.example.com",
            username="lodge@example.com",
            encrypted_password=token,
            from_address="lodge@example.com",
            revision=1,
        )

    def test_randomized_authenticated_encryption(self):
        first = email_config.encrypt_password("same-test-password")
        second = email_config.encrypt_password("same-test-password")
        self.assertNotEqual(first, second)
        self.assertNotIn("same-test-password", first)
        self.assertEqual(email_config.decrypt_password(first), "same-test-password")
        with self.assertRaises(InvalidToken):
            email_config.decrypt_password(first[:-8] + "tampered")

    def test_app_secret_rotation_does_not_affect_dedicated_key(self):
        token = email_config.encrypt_password("smtp-test-password")
        with override_settings(SECRET_KEY="independently-rotated-app-secret"):
            self.assertEqual(email_config.decrypt_password(token), "smtp-test-password")

    def test_missing_or_invalid_key_fails_closed(self):
        for value in ("", "invalid-key"):
            with self.subTest(value=value), override_settings(SMTP_ENCRYPTION_KEY=value):
                with self.assertRaises(ImproperlyConfigured):
                    email_config.encrypt_password("must-not-be-stored-plaintext")

    def test_wrong_key_cannot_decrypt(self):
        token = email_config.encrypt_password("smtp-test-password")
        with override_settings(SMTP_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(InvalidToken):
                email_config.decrypt_password(token)

    def test_legacy_migration_preserves_credential_and_server_settings(self):
        legacy_key = base64.urlsafe_b64encode(
            hashlib.sha256(b"stellar-lodge.smtp.v1:" + settings.SECRET_KEY.encode()).digest()
        )
        secret = "original-smtp-password"
        original = Fernet(legacy_key).encrypt(secret.encode()).decode()
        config = self.config(original)
        output = io.StringIO()
        with self.assertRaises(InvalidToken):
            email_config.decrypt_password(original)
        call_command("reencrypt_smtp_password", legacy=True, stdout=output)
        config.refresh_from_db()
        self.assertEqual(email_config.decrypt_password(config.encrypted_password), secret)
        self.assertNotEqual(original, config.encrypted_password)
        self.assertEqual(config.host, "smtp.example.com")
        self.assertEqual(config.username, "lodge@example.com")
        self.assertEqual(config.from_address, "lodge@example.com")
        self.assertEqual(config.revision, 2)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, str(AuditEvent.objects.get().details))
        self.assertIn("verified", output.getvalue())

    def test_planned_key_rotation_and_retirement(self):
        old_key = settings.SMTP_ENCRYPTION_KEY
        old_token = email_config.encrypt_password("smtp-test-password")
        config = self.config(old_token)
        new_key = Fernet.generate_key().decode()
        with override_settings(
            SMTP_ENCRYPTION_KEY=new_key, SMTP_ENCRYPTION_PREVIOUS_KEYS=[old_key]
        ):
            self.assertEqual(email_config.decrypt_password(old_token), "smtp-test-password")
            call_command("reencrypt_smtp_password", stdout=io.StringIO())
            config.refresh_from_db()
            self.assertEqual(
                Fernet(new_key.encode()).decrypt(config.encrypted_password.encode()),
                b"smtp-test-password",
            )
        with override_settings(SMTP_ENCRYPTION_KEY=new_key, SMTP_ENCRYPTION_PREVIOUS_KEYS=[]):
            self.assertEqual(
                email_config.decrypt_password(config.encrypted_password), "smtp-test-password"
            )
            with self.assertRaises(InvalidToken):
                email_config.decrypt_password(old_token)

    def test_rotation_failure_leaves_original_untouched(self):
        token = Fernet(Fernet.generate_key()).encrypt(b"smtp-test-password").decode()
        config = self.config(token)
        with self.assertRaises(CommandError):
            call_command("reencrypt_smtp_password", stdout=io.StringIO())
        config.refresh_from_db()
        self.assertEqual(config.encrypted_password, token)
        self.assertEqual(config.revision, 1)

    def test_rotation_is_atomic_on_audit_failure(self):
        old_key = Fernet.generate_key()
        token = Fernet(old_key).encrypt(b"smtp-test-password").decode()
        config = self.config(token)
        with override_settings(SMTP_ENCRYPTION_PREVIOUS_KEYS=[old_key.decode()]):
            with patch(
                "lodge.management.commands.reencrypt_smtp_password.audit",
                side_effect=RuntimeError("offline"),
            ):
                with self.assertRaises(RuntimeError):
                    call_command("reencrypt_smtp_password", stdout=io.StringIO())
        config.refresh_from_db()
        self.assertEqual(config.encrypted_password, token)

    def test_current_key_migration_is_idempotent(self):
        config = self.config(email_config.encrypt_password("smtp-test-password"))
        token = config.encrypted_password
        call_command("reencrypt_smtp_password", legacy=True, stdout=io.StringIO())
        config.refresh_from_db()
        self.assertEqual(config.encrypted_password, token)
        self.assertEqual(config.revision, 1)
        self.assertFalse(AuditEvent.objects.exists())
