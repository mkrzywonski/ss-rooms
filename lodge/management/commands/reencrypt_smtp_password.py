"""Rewrap the saved credential without writing or displaying plaintext."""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.views.decorators.debug import sensitive_variables

from lodge.email_config import cipher, decrypt_password, encrypt_password
from lodge.models import EmailConfiguration
from lodge.services import audit


class Command(BaseCommand):
    help = "Re-encrypt the saved SMTP password with the current dedicated key."

    def add_arguments(self, parser):
        parser.add_argument(
            "--legacy",
            action="store_true",
            help="Allow one-time migration from the original Django-secret-derived key.",
        )

    @sensitive_variables("__ALL__")
    @transaction.atomic
    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(19810428, 1)")
        config = EmailConfiguration.objects.select_for_update().filter(pk=1).first()
        if not config or not config.encrypted_password:
            self.stdout.write("No saved SMTP password to re-encrypt.")
            return
        primary = cipher()  # Fail closed if the dedicated key is absent or invalid.
        try:
            primary.decrypt(config.encrypted_password.encode())
        except InvalidToken:
            pass
        else:
            self.stdout.write("SMTP password already uses the current dedicated key.")
            return
        try:
            if options["legacy"]:
                legacy_key = hashlib.sha256(
                    b"stellar-lodge.smtp.v1:" + settings.SECRET_KEY.encode()
                ).digest()
                plaintext = (
                    Fernet(base64.urlsafe_b64encode(legacy_key))
                    .decrypt(config.encrypted_password.encode())
                    .decode()
                )
            else:
                plaintext = decrypt_password(config.encrypted_password)
        except InvalidToken:
            raise CommandError("Unable to decrypt saved SMTP password; no changes made.") from None
        replacement = encrypt_password(plaintext)
        if not hmac.compare_digest(plaintext.encode(), primary.decrypt(replacement.encode())):
            raise CommandError("SMTP credential verification failed; no changes made.")
        config.encrypted_password = replacement
        config.revision += 1
        config.save(update_fields=["encrypted_password", "revision"])
        audit(None, "smtp_credential_reencrypted", config)
        self.stdout.write(
            "SMTP password re-encrypted and verified; server/account settings preserved."
        )
