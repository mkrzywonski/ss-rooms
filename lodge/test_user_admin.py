from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from . import services, user_admin
from .models import AuditEvent, Commitment, Notification, Problem, User
from .tests import Fixtures


class UserAdministrationTests(Fixtures, TestCase):
    def data(self, member=None, **overrides):
        data = {
            "name": member.name if member else "New Observer",
            "email": member.email if member else "new-observer@example.com",
            "connection": member.connection if member else "Stellar Skies member",
            "status": member.status if member else "approved",
            "is_active": member.is_active if member else True,
            "is_staff": member.is_staff if member else False,
            "password1": "",
            "password2": "",
            "send_setup": False,
            "revision": member.admin_revision if member else None,
        }
        data.update(overrides)
        return data

    def test_contact_fields_default_to_blank(self):
        member = User.objects.create_user("contact-defaults@example.com", name="Observer")
        for field in ("address", "phone_number", "observatory_number"):
            self.assertEqual(getattr(member, field), "")

    def test_admin_can_create_edit_and_clear_contact_fields(self):
        fields = {
            "address": "123 Country Road\nAustin, TX 78701",
            "phone_number": "+1 (512) 555-0123 ext. 4",
            "observatory_number": "07",
        }
        member = user_admin.save_member(self.admin, self.data(**fields))
        member.refresh_from_db()
        for key, value in fields.items():
            self.assertEqual(getattr(member, key), value)
        member = user_admin.save_member(
            self.admin, self.data(member, **{**fields, "observatory_number": "A-3"}), member.pk
        )
        self.assertEqual(member.observatory_number, "A-3")
        member = user_admin.save_member(
            self.admin, self.data(member, **dict.fromkeys(fields, "")), member.pk
        )
        member.refresh_from_db()
        for key in fields:
            self.assertEqual(getattr(member, key), "")

    def test_contact_fields_on_admin_form_and_edit_post(self):
        self.client.force_login(self.admin)
        path = reverse("member_edit", args=[self.bob.pk])
        response = self.client.get(path)
        for field in ("address", "phone_number", "observatory_number"):
            self.assertContains(response, f'name="{field}"')
        fields = {
            "address": "123 Country Road",
            "phone_number": "+1 512 555 0123",
            "observatory_number": "07",
        }
        response = self.client.post(path, self.data(self.bob, **fields))
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        form = user_admin.MemberForm(member=self.bob, actor=self.admin)
        for key, value in fields.items():
            self.assertEqual(getattr(self.bob, key), value)
            self.assertEqual(form.initial[key], value)

    def test_contact_field_limits(self):
        for key, length in (("address", 2000), ("phone_number", 40), ("observatory_number", 40)):
            with self.subTest(field=key):
                form = user_admin.MemberForm(
                    self.data(self.bob, **{key: "x" * (length + 1)}),
                    member=self.bob,
                    actor=self.admin,
                )
                self.assertFalse(form.is_valid())
                self.assertIn(key, form.errors)
                with self.assertRaises(ValidationError):
                    user_admin.save_member(
                        self.admin, self.data(self.bob, **{key: "x" * (length + 1)}), self.bob.pk
                    )

    def test_omitted_contact_fields_preserve_existing_values(self):
        self.bob.address = "123 Country Road"
        self.bob.phone_number = "+1 512 555 0123"
        self.bob.observatory_number = "07"
        self.bob.save()
        member = user_admin.save_member(self.admin, self.data(self.bob), self.bob.pk)
        self.assertEqual(member.address, "123 Country Road")
        self.assertEqual(member.phone_number, "+1 512 555 0123")
        self.assertEqual(member.observatory_number, "07")

    def test_admin_can_add_an_approved_user_and_queue_setup(self):
        member = user_admin.save_member(self.admin, self.data())
        self.assertEqual(member.status, "approved")
        self.assertFalse(member.has_usable_password())
        self.assertFalse(member.is_superuser)
        message = Notification.objects.get(recipient=member.email)
        self.assertIn("/accounts/reset/", message.body)
        self.assertEqual(AuditEvent.objects.get().action, "member_created")

    def test_admin_created_user_can_follow_setup_and_choose_password(self):
        member = user_admin.save_member(self.admin, self.data())
        message = Notification.objects.get(recipient=member.email)
        link = next(line for line in message.body.splitlines() if "/accounts/reset/" in line)
        response = self.client.get(link, follow=True)
        self.assertTrue(response.context["validlink"])
        response = self.client.post(
            response.request["PATH_INFO"],
            {
                "new_password1": "new-observer-strong-password",
                "new_password2": "new-observer-strong-password",
            },
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertTrue(member.check_password("new-observer-strong-password"))

    def test_new_password_is_hashed_and_not_audited_or_emailed(self):
        password = "another-strong-observer-password"
        member = user_admin.save_member(
            self.admin, self.data(password1=password, password2=password)
        )
        self.assertNotEqual(member.password, password)
        self.assertTrue(member.check_password(password))
        self.assertNotIn(password, str(AuditEvent.objects.get().details))
        self.assertFalse(Notification.objects.filter(body__contains=password).exists())

    def test_edit_profile_preserves_password_and_normalizes_email(self):
        previous = self.bob.password
        member = user_admin.save_member(
            self.admin, self.data(self.bob, name="Robert", email="ROBERT@Example.com"), self.bob.pk
        )
        self.assertEqual(member.name, "Robert")
        self.assertEqual(member.email, "robert@example.com")
        self.assertEqual(member.password, previous)
        self.assertEqual(member.admin_revision, 2)
        self.assertTrue(
            Notification.objects.filter(
                recipient="bob@example.com", subject__contains="email changed"
            ).exists()
        )

    def test_member_password_can_be_changed(self):
        password = "new-robert-account-password"
        user_admin.save_member(
            self.admin, self.data(self.bob, password1=password, password2=password), self.bob.pk
        )
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.check_password(password))

    def test_duplicate_email_case_insensitive(self):
        with self.assertRaises(ValidationError):
            user_admin.save_member(self.admin, self.data(email="ALICE@EXAMPLE.COM"))
        self.assertEqual(User.objects.count(), 3)

    def test_weak_and_mismatched_passwords_rejected(self):
        for overrides in (
            {"password1": "short", "password2": "short"},
            {"password1": "strong-long-account-password", "password2": "different"},
        ):
            with self.assertRaises(ValidationError):
                user_admin.save_member(self.admin, self.data(**overrides))
        self.assertEqual(User.objects.count(), 3)

    def test_member_cannot_create_edit_or_delete_accounts(self):
        for operation in (
            lambda: user_admin.save_member(self.alice, self.data()),
            lambda: user_admin.save_member(self.alice, self.data(self.bob), self.bob.pk),
            lambda: user_admin.delete_member(self.alice, self.bob.pk, 1),
        ):
            with self.assertRaises(PermissionDenied):
                operation()
        self.client.force_login(self.alice)
        for path in (
            "/manage/members/new/",
            f"/manage/members/{self.bob.pk}/edit/",
            f"/manage/members/{self.bob.pk}/delete/",
        ):
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post(path, {}).status_code, 403)

    def test_admin_cannot_remove_own_access(self):
        for changes in ({"is_staff": False}, {"is_active": False}, {"status": "suspended"}):
            with self.assertRaises(ValidationError):
                user_admin.save_member(self.admin, self.data(self.admin, **changes), self.admin.pk)
        with self.assertRaises(ValidationError):
            user_admin.delete_member(self.admin, self.admin.pk, 1)

    def test_ordinary_admin_cannot_edit_superuser(self):
        self.bob.is_staff = True
        self.bob.save(update_fields=["is_staff"])
        with self.assertRaises(PermissionDenied):
            user_admin.save_member(self.bob, self.data(self.admin, name="Changed"), self.admin.pk)
        with self.assertRaises(ValidationError):
            user_admin.delete_member(self.bob, self.admin.pk, 1)

    def test_administrators_must_be_active_and_approved(self):
        with self.assertRaises(ValidationError):
            user_admin.save_member(self.admin, self.data(is_staff=True, status="suspended"))

    def test_superuser_cannot_be_set_through_extra_fields(self):
        member = user_admin.save_member(self.admin, self.data(is_superuser=True))
        self.assertFalse(member.is_superuser)

    def test_stale_edit_and_delete_are_rejected(self):
        data = self.data(self.bob)
        services.change_admin_role(self.admin, self.bob.pk, True)
        with self.assertRaises(ValidationError):
            user_admin.save_member(self.admin, data, self.bob.pk)
        with self.assertRaises(ValidationError):
            user_admin.delete_member(self.admin, self.bob.pk, 1)

    def test_deleted_account_is_hidden_and_cannot_log_in(self):
        self.client.force_login(self.bob)
        user_admin.delete_member(self.admin, self.bob.pk, 1)
        self.bob.refresh_from_db()
        self.assertIsNotNone(self.bob.deleted_at)
        self.assertFalse(self.bob.is_active)
        self.assertFalse(self.bob.has_usable_password())
        self.assertEqual(self.client.get("/").status_code, 302)
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get("/manage/members/"), self.bob.email)
        self.assertContains(self.client.get("/manage/members/?deleted=1"), self.bob.email)

    def test_deletion_preserves_report_and_audit_history(self):
        problem = services.create_problem(
            self.bob,
            {
                "title": "Faucet",
                "description": "Drips",
                "location": "Kitchen",
                "resource": None,
                "urgency": "routine",
            },
            [],
        )
        user_admin.delete_member(self.admin, self.bob.pk, 1)
        problem.refresh_from_db()
        self.assertEqual(problem.reporter_id, self.bob.pk)
        self.assertTrue(AuditEvent.objects.filter(actor=self.bob).exists())

    def test_deletion_requires_resolving_upcoming_reservations(self):
        booking = self.book(self.bob)
        with self.assertRaises(ValidationError):
            user_admin.delete_member(self.admin, self.bob.pk, 1)
        self.assertTrue(booking.active)
        self.bob.refresh_from_db()
        self.assertIsNone(self.bob.deleted_at)

    def test_deletion_cancels_plans_and_unassigns_problems(self):
        plan = Commitment.objects.create(member=self.bob, item=self.item, expected_date=self.start)
        problem = Problem.objects.create(
            reporter=self.alice,
            title="Faucet",
            location="Kitchen",
            description="Drips",
            working_on=self.bob,
        )
        user_admin.delete_member(self.admin, self.bob.pk, 1)
        plan.refresh_from_db()
        problem.refresh_from_db()
        self.assertEqual(plan.status, "canceled")
        self.assertIsNone(problem.working_on)

    def test_deleted_account_cannot_be_edited_or_approved(self):
        user_admin.delete_member(self.admin, self.bob.pk, 1)
        with self.assertRaises(ValidationError):
            user_admin.save_member(self.admin, self.data(self.bob), self.bob.pk)
        with self.assertRaises(ValidationError):
            services.decide_member(self.admin, self.bob.pk, "approved")
        with self.assertRaises(ValidationError):
            user_admin.save_member(self.admin, self.data(email=self.bob.email))

    def test_deleted_account_reset_token_is_invalidated(self):
        token = default_token_generator.make_token(self.bob)
        user_admin.delete_member(self.admin, self.bob.pk, 1)
        self.bob.refresh_from_db()
        self.assertFalse(default_token_generator.check_token(self.bob, token))

    def test_account_creation_and_mail_queue_are_atomic(self):
        with patch(
            "lodge.user_admin.queue_password_setup", side_effect=RuntimeError("queue unavailable")
        ):
            with self.assertRaises(RuntimeError):
                user_admin.save_member(self.admin, self.data())
        self.assertEqual(User.objects.count(), 3)
        self.assertFalse(AuditEvent.objects.exists())

    def test_admin_pages_and_create_post(self):
        self.client.force_login(self.admin)
        for path in (
            "/manage/members/new/",
            f"/manage/members/{self.bob.pk}/edit/",
            f"/manage/members/{self.bob.pk}/delete/",
        ):
            self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.post(
            "/manage/members/new/", {**self.data(), "revision": "", "is_active": "on"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email="new-observer@example.com").exists())

    def test_edit_page_does_not_disclose_password_hash(self):
        self.client.force_login(self.admin)
        self.assertNotContains(
            self.client.get(reverse("member_edit", args=[self.bob.pk])), self.bob.password
        )

    def test_delete_requires_confirmation_and_get_is_readonly(self):
        self.client.force_login(self.admin)
        path = reverse("member_delete", args=[self.bob.pk])
        self.client.get(path)
        self.client.post(path, {"revision": 1})
        self.bob.refresh_from_db()
        self.assertIsNone(self.bob.deleted_at)
        response = self.client.post(path, {"revision": 1, "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        self.assertIsNotNone(self.bob.deleted_at)

    def test_changing_own_password_keeps_current_session(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("member_edit", args=[self.admin.pk]),
            {
                **self.data(
                    self.admin,
                    password1="updated-admin-password-safe",
                    password2="updated-admin-password-safe",
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/manage/members/").status_code, 200)


class UserDeletionConcurrencyTests(Fixtures, TransactionTestCase):
    def test_account_deletion_cannot_race_with_new_reservation(self):
        barrier = Barrier(2)

        def book():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                services.save_booking(User.objects.get(pk=self.bob.pk), [self.row()])
                return "booked"
            except PermissionDenied:
                return "booking_blocked"
            finally:
                connections.close_all()

        def delete():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                user_admin.delete_member(User.objects.get(pk=self.admin.pk), self.bob.pk, 1)
                return "deleted"
            except ValidationError:
                return "deletion_blocked"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(book), executor.submit(delete)]
            results = {result.result(timeout=15) for result in results}
        self.assertIn(results, ({"booked", "deletion_blocked"}, {"deleted", "booking_blocked"}))
