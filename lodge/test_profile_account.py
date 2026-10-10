import logging
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from . import profiles
from .logging import RedactTokenPaths
from .models import AuditEvent, Notification
from .tests import Fixtures


class ProfileAccountTests(Fixtures, TestCase):
    old_password = "old-observer-account-password"
    new_password = "new-observer-account-password"

    def setUp(self):
        super().setUp()
        self.bob.set_password(self.old_password)
        self.bob.save()
        self.client.force_login(self.bob)

    def password_data(self, **changes):
        data = {
            "action": "password",
            "old_password": self.old_password,
            "new_password1": self.new_password,
            "new_password2": self.new_password,
            "revision": self.bob.admin_revision,
        }
        data.update(changes)
        return data

    def email_data(self, **changes):
        data = {
            "action": "email",
            "current_password": self.old_password,
            "new_email": "new-bob@example.com",
            "revision": self.bob.admin_revision,
        }
        data.update(changes)
        return data

    def request_email(self, **changes):
        response = self.client.post(reverse("my_profile"), self.email_data(**changes))
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        notification = Notification.objects.filter(recipient=self.bob.pending_email).latest("pk")
        url = next(
            line for line in notification.body.splitlines() if "/account/email/confirm/" in line
        )
        token = url.rstrip("/").rsplit("/", 1)[1]
        return url, token

    def test_name_is_only_profile_link_in_main_navigation(self):
        response = self.client.get("/")
        self.assertContains(response, f'class="user-name" href="{reverse("my_profile")}"')
        self.assertContains(response, f'aria-label="My profile">{self.bob.name}</a>')
        self.assertEqual(response.content.decode().count(f'href="{reverse("my_profile")}"'), 1)
        self.assertNotContains(response, ">My profile</a>")

    def test_observatory_edit_and_clear_preserves_text_identifiers(self):
        for value in ("07", "A-3", ""):
            data = {key: getattr(self.bob, key) for key in profiles.CONTACT_FIELDS}
            data.update(
                observatory_number=value, revision=self.bob.admin_revision, action="profile"
            )
            self.assertEqual(self.client.post(reverse("my_profile"), data).status_code, 302)
            self.bob.refresh_from_db()
            self.assertEqual(self.bob.observatory_number, value)
        data["observatory_number"] = "x" * 41
        data["revision"] = self.bob.admin_revision
        self.assertEqual(self.client.post(reverse("my_profile"), data).status_code, 200)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.observatory_number, "")

    def test_password_change_hashed_current_session_kept_others_invalidated(self):
        other = Client()
        other.force_login(self.bob)
        response = self.client.post(reverse("my_profile"), self.password_data())
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        self.assertNotEqual(self.bob.password, self.new_password)
        self.assertTrue(self.bob.check_password(self.new_password))
        self.assertFalse(self.bob.check_password(self.old_password))
        self.assertEqual(self.client.get(reverse("my_profile")).status_code, 200)
        self.assertEqual(other.get("/").status_code, 302)
        self.assertTrue(self.client.login(email=self.bob.email, password=self.new_password))
        self.assertNotIn(self.new_password, str(AuditEvent.objects.get().details))
        self.assertFalse(Notification.objects.filter(body__contains=self.new_password).exists())

    def test_password_validation_errors_never_redisplay_password(self):
        for changes in (
            {"old_password": "incorrect-password"},
            {"new_password1": "short", "new_password2": "short"},
            {"new_password2": "different-password"},
        ):
            response = self.client.post(reverse("my_profile"), self.password_data(**changes))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, f'value="{self.new_password}"')
            self.assertNotContains(response, f'value="{self.old_password}"')
            self.bob.refresh_from_db()
            self.assertTrue(self.bob.check_password(self.old_password))

    def test_password_change_stale_form_and_queue_failure_rollback(self):
        with self.assertRaises(ValidationError):
            profiles.change_password(self.bob, self.password_data(revision=0))
        with patch("lodge.profiles.queue_email", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                profiles.change_password(self.bob, self.password_data())
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.check_password(self.old_password))
        self.assertFalse(AuditEvent.objects.exists())

    def test_email_request_keeps_old_signin_until_confirmed_and_get_is_readonly(self):
        url, token = self.request_email(new_email="NEW-BOB@Example.com")
        self.assertEqual(self.bob.email, "bob@example.com")
        self.assertEqual(self.bob.pending_email, "new-bob@example.com")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "bob@example.com")
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "new-bob@example.com")
        self.assertEqual(self.bob.pending_email, "")
        self.assertEqual(self.bob.email_change_token, "")
        self.assertEqual(self.client.get(reverse("my_profile")).status_code, 200)
        self.assertTrue(self.client.login(email="new-bob@example.com", password=self.old_password))
        self.assertFalse(self.client.login(email="bob@example.com", password=self.old_password))
        self.assertTrue(
            Notification.objects.filter(
                recipient="bob@example.com", subject__endswith="email address changed"
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient="new-bob@example.com", subject__endswith="email address changed"
            ).exists()
        )
        with self.assertRaises(ValidationError):
            profiles.confirm_email_change(self.bob, token)

    def test_email_request_requires_current_password_unique_valid_new_email(self):
        for changes in (
            {"current_password": "wrong-password"},
            {"new_email": "ALICE@example.com"},
            {"new_email": "bob@example.com"},
            {"new_email": "not-an-email"},
            {"revision": 0},
        ):
            response = self.client.post(reverse("my_profile"), self.email_data(**changes))
            self.assertEqual(response.status_code, 200)
            self.bob.refresh_from_db()
            self.assertEqual(self.bob.pending_email, "")
        self.assertFalse(Notification.objects.exists())

    def test_canceled_or_replaced_requests_invalidate_old_links(self):
        _, first = self.request_email()
        self.request_email(new_email="other-bob@example.com")
        with self.assertRaises(ValidationError):
            profiles.confirm_email_change(self.bob, first)
        _, latest = self.request_email()
        self.client.post(
            reverse("my_profile"), {"action": "cancel_email", "revision": self.bob.admin_revision}
        )
        self.bob.refresh_from_db()
        with self.assertRaises(ValidationError):
            profiles.confirm_email_change(self.bob, latest)
        self.assertEqual(self.bob.pending_email, "")

    def test_email_confirmation_requires_owner_and_valid_token(self):
        url, token = self.request_email()
        self.client.force_login(self.alice)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "This link belongs to a different account", status_code=400)
        self.assertNotContains(response, "invalid or expired", status_code=400)
        self.assertNotContains(response, self.bob.pending_email, status_code=400)
        self.assertEqual(self.client.post(url).status_code, 400)
        self.client.force_login(self.bob)
        self.assertEqual(
            self.client.post(reverse("confirm_email_change", args=["tampered"])).status_code, 400
        )
        with patch("django.core.signing.time.time", return_value=10**12):
            with self.assertRaises(ValidationError):
                profiles.confirm_email_change(self.bob, token)

    def test_confirmation_works_after_switching_to_requesting_account(self):
        url, _ = self.request_email()
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(url).status_code, 400)
        self.client.logout()
        self.assertTrue(self.client.login(email=self.bob.email, password=self.old_password))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm email change")
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "bob@example.com")
        self.assertEqual(self.client.post(url).status_code, 302)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "new-bob@example.com")

    def test_email_confirmation_rechecks_unique_address(self):
        _, token = self.request_email()
        self.alice.email = self.bob.pending_email
        self.alice.save()
        with self.assertRaises(ValidationError):
            profiles.confirm_email_change(self.bob, token)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "bob@example.com")

    def test_password_reset_invalidates_email_change_link(self):
        _, token = self.request_email()
        # A password reset changes this hash too, even without using My profile.
        self.bob.set_password("reset-observer-account-password")
        self.bob.save()
        with self.assertRaises(ValidationError):
            profiles.confirm_email_change(self.bob, token)

    def test_request_and_confirmation_email_queue_failures_atomic(self):
        with patch("lodge.profiles.queue_email", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                profiles.request_email_change(self.bob, self.email_data())
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.pending_email, "")
        _, token = self.request_email()
        with patch("lodge.profiles.queue_email", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                profiles.confirm_email_change(self.bob, token)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "bob@example.com")

    def test_password_reset_token_invalid_after_email_change(self):
        reset = default_token_generator.make_token(self.bob)
        _, token = self.request_email()
        member = profiles.confirm_email_change(self.bob, token)
        self.assertFalse(default_token_generator.check_token(member, reset))

    def test_csrf_required_for_password_email_and_confirmation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.bob)
        for data in (self.password_data(), self.email_data()):
            self.assertEqual(client.post(reverse("my_profile"), data).status_code, 403)
        url, _ = self.request_email()
        self.assertEqual(client.post(url, {}).status_code, 403)

    def test_confirmation_policy_and_csrf_checked_https_submission(self):
        url, _ = self.request_email()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.bob)
        response = client.get(url, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        csrf_token = client.cookies["csrftoken"].value
        data = {"csrfmiddlewaretoken": csrf_token}
        # A valid token must not bypass origin checks.
        for origin in ("null", "https://untrusted.example"):
            self.assertEqual(
                client.post(url, data, secure=True, HTTP_ORIGIN=origin).status_code, 403
            )
        # strict-origin preserves the same-origin header for browser form POSTs.
        response = client.post(url, data, secure=True, HTTP_ORIGIN="https://testserver")
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "new-bob@example.com")

    def test_confirmation_policy_preserves_https_referer_fallback(self):
        url, _ = self.request_email()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.bob)
        response = client.get(url, secure=True)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        response = client.post(
            url,
            {"csrfmiddlewaretoken": client.cookies["csrftoken"].value},
            secure=True,
            HTTP_REFERER="https://testserver/",
        )
        self.assertEqual(response.status_code, 302)

    def test_invalid_confirmation_also_uses_csrf_compatible_policy(self):
        response = self.client.get(reverse("confirm_email_change", args=["tampered"]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")

    def test_email_change_tokens_redacted_from_logs(self):
        record = logging.LogRecord(
            "django", 30, "", 1, "Bad Request: %s", ("/account/email/confirm/secret-token/",), None
        )
        RedactTokenPaths().filter(record)
        self.assertNotIn("secret-token", record.getMessage())
        self.assertIn("[redacted]", record.getMessage())
