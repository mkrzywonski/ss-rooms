import re
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import email_config
from .models import AuditEvent, Consumable, EmailConfiguration, Notification
from .notifications import deliver_one
from .tests import Fixtures


class EmailConfigurationTests(Fixtures, TestCase):
    def data(self, **changes):
        config = email_config.current_configuration()
        result = {
            "host": "smtp.example.com",
            "port": 587,
            "security": "starttls",
            "username": "lodge@example.com",
            "password": "",
            "clear_password": False,
            "from_address": "lodge@example.com",
            "from_name": "Stellar Skies Lodge",
            "revision": config.revision,
        }
        result.update(changes)
        return result

    def test_credentials_encrypted_and_never_rendered_or_audited(self):
        secret = "private-smtp-app-password"
        config = email_config.save_configuration(self.admin, self.data(password=secret))
        self.assertNotIn(secret, config.encrypted_password)
        self.assertEqual(email_config.decrypt_password(config.encrypted_password), secret)
        self.assertNotIn(secret, str(AuditEvent.objects.get().details))
        self.client.force_login(self.admin)
        response = self.client.get(reverse("email_settings"))
        self.assertContains(response, "SMTP password saved")
        self.assertNotContains(response, secret)
        self.assertNotContains(response, config.encrypted_password)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_unchanged_password_preserved_and_can_be_cleared(self):
        config = email_config.save_configuration(self.admin, self.data(password="smtp-secret"))
        saved = config.encrypted_password
        config = email_config.save_configuration(self.admin, self.data(from_name="The Lodge"))
        self.assertEqual(config.encrypted_password, saved)
        config = email_config.save_configuration(self.admin, self.data(clear_password=True))
        self.assertEqual(config.encrypted_password, "")

    def test_host_or_account_change_requires_explicit_password_decision(self):
        email_config.save_configuration(self.admin, self.data(password="smtp-secret"))
        for changes in (
            {"host": "other.example.com"},
            {"username": "other@example.com"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                email_config.save_configuration(self.admin, self.data(**changes))
        config = email_config.save_configuration(
            self.admin, self.data(host="other.example.com", password="new-secret")
        )
        self.assertEqual(email_config.decrypt_password(config.encrypted_password), "new-secret")

    def test_stale_settings_rejected(self):
        original = self.data()
        email_config.save_configuration(self.admin, original)
        with self.assertRaises(ValidationError):
            email_config.save_configuration(self.admin, original)

    def test_invalid_settings_rejected(self):
        for changes in (
            {"host": ""},
            {"host": "https://smtp.example.com"},
            {"host": "bad host"},
            {"port": 0},
            {"port": 65536},
            {"security": "none"},
            {"from_address": "bad"},
            {"from_name": "Lodge\r\nBcc: thief@example.com"},
            {"password": "secret", "clear_password": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                email_config.save_configuration(self.admin, self.data(**changes))
        self.assertFalse(EmailConfiguration.objects.exists())

    def test_test_message_is_queued_to_acting_admin_only(self):
        email_config.save_configuration(self.admin, self.data(password="secret"), send_test=True)
        message = Notification.objects.get()
        self.assertEqual(message.recipient, self.admin.email)
        self.assertTrue(message.subject.endswith(email_config.TEST_SUBJECT))
        self.assertNotIn("secret", message.body)

    def test_test_requires_server_and_sender(self):
        for changes in ({"host": ""}, {"from_address": ""}):
            with self.assertRaises(ValidationError):
                email_config.save_configuration(self.admin, self.data(**changes), send_test=True)
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(EmailConfiguration.objects.exists())

    def test_settings_and_test_queue_atomic(self):
        with patch("lodge.email_config.queue_email", side_effect=RuntimeError("queue offline")):
            with self.assertRaises(RuntimeError):
                email_config.save_configuration(self.admin, self.data(), send_test=True)
        self.assertFalse(EmailConfiguration.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_admin_post_and_password_never_redisplayed_on_error(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("email_settings"), {**self.data(password="hidden-secret"), "action": "test"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertContains(self.client.get(reverse("email_settings")), "Queued")
        response = self.client.post(
            reverse("email_settings"), self.data(port=0, password="another-hidden-secret")
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "another-hidden-secret")

    def test_member_and_anonymous_permissions(self):
        self.assertEqual(self.client.get(reverse("email_settings")).status_code, 302)
        self.client.force_login(self.alice)
        for path in ("email_settings", "admin_supplies", "admin_bookings"):
            self.assertEqual(self.client.get(reverse(path)).status_code, 403)
            self.assertEqual(self.client.post(reverse(path), self.data()).status_code, 403)
        with self.assertRaises(PermissionDenied):
            email_config.save_configuration(self.alice, self.data())

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("email_settings"), self.data()).status_code, 403)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="default@example.com",
    )
    def test_unconfigured_bootstrap_preserves_existing_backend(self):
        with patch("lodge.email_config.get_connection") as connect:
            _, sender = email_config.transport()
            connect.assert_called_once_with()
            self.assertEqual(sender, "default@example.com")

    def test_smtp_transport_settings_and_live_updates(self):
        email_config.save_configuration(self.admin, self.data(password="secret"))
        with patch("lodge.email_config.get_connection") as connect:
            _, sender = email_config.transport()
            values = connect.call_args.kwargs
            self.assertEqual(values["host"], "smtp.example.com")
            self.assertEqual(values["password"], "secret")
            self.assertTrue(values["use_tls"])
            self.assertFalse(values["use_ssl"])
            self.assertEqual(sender, "Stellar Skies Lodge <lodge@example.com>")
        email_config.save_configuration(self.admin, self.data(security="ssl", port=465))
        with patch("lodge.email_config.get_connection") as connect:
            email_config.transport()
            values = connect.call_args.kwargs
            self.assertTrue(values["use_ssl"])
            self.assertFalse(values["use_tls"])
            self.assertEqual(values["port"], 465)

    def test_legacy_mode_submission_cannot_disable_smtp(self):
        email_config.save_configuration(self.admin, self.data(mode="file"))
        with patch("lodge.email_config.get_connection") as connect:
            email_config.transport()
            self.assertEqual(
                connect.call_args.kwargs["backend"],
                "django.core.mail.backends.smtp.EmailBackend",
            )

    def test_email_page_only_offers_smtp_configuration(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("email_settings"))
        self.assertContains(response, 'name="host"')
        self.assertNotContains(response, 'name="mode"')
        self.assertNotContains(response, "Current mode")
        self.assertNotContains(response, "Capture to private files")
        self.assertNotContains(response, "Deployment defaults")

    def test_worker_uses_live_connection_and_sender(self):
        email_config.save_configuration(self.admin, self.data(password="secret"), send_test=True)
        with patch("lodge.email_config.get_connection") as connect:
            with patch("lodge.notifications.send_mail", return_value=1) as send:
                self.assertTrue(deliver_one())
                self.assertEqual(send.call_args.args[2], "Stellar Skies Lodge <lodge@example.com>")
                self.assertIs(send.call_args.kwargs["connection"], connect.return_value)
                self.assertEqual(connect.call_args.kwargs["password"], "secret")
        self.assertIsNotNone(Notification.objects.get().sent_at)

    def test_failure_does_not_disclose_password(self):
        email_config.save_configuration(
            self.admin, self.data(password="private-secret"), send_test=True
        )
        with patch("lodge.notifications.send_mail", side_effect=OSError("private-secret")):
            with self.assertLogs("lodge.notifications", level="WARNING") as logs:
                deliver_one()
        self.assertNotIn("private-secret", str(logs.output))
        self.assertEqual(Notification.objects.get().last_error, "OSError")

    def test_tampered_ciphertext_rejected(self):
        encrypted = email_config.encrypt_password("secret")
        with self.assertRaises(InvalidToken):
            email_config.decrypt_password(encrypted[:-8] + "tampered")
        with override_settings(SMTP_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(InvalidToken):
                email_config.decrypt_password(encrypted)


class AdminNavigationTests(Fixtures, TestCase):
    def test_admin_functions_grouped_under_menu(self):
        self.client.force_login(self.admin)
        response = self.client.get("/")
        self.assertContains(response, "<summary>Admin</summary>")
        html = response.content.decode()
        menu = re.search(r'<details class="admin-menu">.*?</details>', html).group()
        for name in ("members", "resources", "admin_bookings", "admin_supplies", "email_settings"):
            self.assertIn(reverse(name), menu)
        remainder = html.replace(menu, "")
        self.assertNotIn(reverse("members"), remainder)
        self.assertNotIn(reverse("resources"), remainder)
        for name in ("calendar", "problems", "supplies"):
            self.assertIn(reverse(name), remainder)
        self.client.force_login(self.alice)
        self.assertNotContains(self.client.get("/"), "<summary>Admin</summary>")

    def test_regular_supplies_page_has_no_admin_controls(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("supplies"))
        self.assertNotContains(response, reverse("consumable_new"))
        self.assertNotContains(response, reverse("consumable_edit", args=[self.item.pk]))
        response = self.client.get(reverse("admin_supplies"))
        self.assertContains(response, reverse("consumable_new"))
        self.assertContains(response, reverse("consumable_edit", args=[self.item.pk]))

    def test_supply_create_and_edit_return_to_admin_list(self):
        self.client.force_login(self.admin)
        data = {
            "name": "Dish soap", "description": "For the kitchen",
            "location": "Under the sink", "active": "on",
        }
        response = self.client.post(reverse("consumable_new"), data)
        self.assertRedirects(response, reverse("admin_supplies"))
        created = Consumable.objects.get(name="Dish soap")
        response = self.client.post(
            reverse("consumable_edit", args=[created.pk]),
            {**data, "description": "Unscented dish soap"},
        )
        self.assertRedirects(response, reverse("admin_supplies"))
        created.refresh_from_db()
        self.assertEqual(created.description, "Unscented dish soap")
        self.assertContains(self.client.get(reverse("admin_supplies")), reverse("consumable_new"))

    def test_other_members_booking_admin_controls_only_in_admin_view(self):
        booking = self.book(self.bob)
        self.client.force_login(self.admin)
        normal = self.client.get(reverse("booking_detail", args=[booking.pk]))
        self.assertNotContains(normal, "Cancel entire booking")
        self.assertNotContains(normal, "Change booking")
        administrative = self.client.get(reverse("admin_booking_detail", args=[booking.pk]))
        self.assertContains(administrative, "Cancel entire booking")
        self.assertContains(administrative, "Change booking")
        self.assertContains(
            self.client.get(reverse("admin_bookings")),
            reverse("admin_booking_detail", args=[booking.pk]),
        )
        self.client.force_login(self.alice)
        self.assertEqual(
            self.client.get(reverse("admin_booking_detail", args=[booking.pk])).status_code, 403
        )

    def test_own_booking_still_editable_from_regular_pages(self):
        booking = self.book(self.admin)
        self.client.force_login(self.admin)
        self.assertContains(
            self.client.get(reverse("booking_detail", args=[booking.pk])), "Change booking"
        )
