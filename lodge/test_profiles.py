from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from . import profiles, user_admin
from .models import AuditEvent, User
from .tests import Fixtures


class ProfilePrivacyTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.bob.address = "PRIVATE-ADDRESS 123 Country Road"
        self.bob.phone_number = "PRIVATE-PHONE +1 555 0123"
        self.bob.save()

    def data(self, member=None, **changes):
        member = member or self.bob
        result = {key: getattr(member, key) for key in profiles.CONTACT_FIELDS}
        result["revision"] = member.admin_revision
        result.update(changes)
        return result

    def admin_data(self, member, **changes):
        result = {
            key: getattr(member, key)
            for key in (
                "name",
                "email",
                "connection",
                "status",
                "is_active",
                "is_staff",
                "address",
                "phone_number",
                "observatory_number",
                "share_address",
                "share_phone_number",
            )
        }
        result.update(password1="", password2="", send_setup=False, revision=member.admin_revision)
        result.update(changes)
        return result

    def test_contact_sharing_defaults_private(self):
        self.assertFalse(self.bob.share_address)
        self.assertFalse(self.bob.share_phone_number)

    def test_private_values_omitted_from_directory_html_and_context(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("member_directory"))
        self.assertNotContains(response, self.bob.address)
        self.assertNotContains(response, self.bob.phone_number)
        entry = next(row for row in response.context["entries"] if row["pk"] == self.bob.pk)
        self.assertEqual(entry["visible_address"], "")
        self.assertEqual(entry["visible_phone_number"], "")
        self.assertNotIn("address", entry)
        self.assertNotIn("phone_number", entry)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_sharing_choices_independent(self):
        self.client.force_login(self.alice)
        profiles.save_profile(self.bob, self.data(share_address=True))
        response = self.client.get(reverse("member_directory"))
        self.assertContains(response, self.bob.address)
        self.assertNotContains(response, self.bob.phone_number)
        self.bob.refresh_from_db()
        profiles.save_profile(self.bob, self.data(share_address=False, share_phone_number=True))
        response = self.client.get(reverse("member_directory"))
        self.assertNotContains(response, self.bob.address)
        self.assertContains(response, self.bob.phone_number)

    def test_revocation_hides_details_without_deleting_them(self):
        profiles.save_profile(self.bob, self.data(share_address=True, share_phone_number=True))
        self.bob.refresh_from_db()
        profiles.save_profile(self.bob, self.data(share_address=False, share_phone_number=False))
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.address)
        self.assertTrue(self.bob.phone_number)
        self.client.force_login(self.alice)
        response = self.client.get(reverse("member_directory"))
        self.assertNotContains(response, self.bob.address)
        self.assertNotContains(response, self.bob.phone_number)

    def test_admin_regular_directory_respects_same_privacy(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("member_directory"))
        self.assertNotContains(response, self.bob.address)
        self.assertNotContains(response, self.bob.phone_number)
        response = self.client.get(reverse("member_edit", args=[self.bob.pk]))
        self.assertContains(response, self.bob.address)
        self.assertContains(response, self.bob.phone_number)

    def test_owner_can_edit_contact_details_and_preferences(self):
        self.client.force_login(self.bob)
        response = self.client.get(reverse("my_profile"))
        self.assertContains(response, 'type="checkbox" name="share_address"')
        self.assertContains(response, 'type="checkbox" name="share_phone_number"')
        self.assertContains(response, self.bob.address)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response = self.client.post(
            reverse("my_profile"),
            self.data(address="New address", phone_number="+1 (555) 999", share_address=True),
        )
        self.assertEqual(response.status_code, 302)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.address, "New address")
        self.assertEqual(self.bob.phone_number, "+1 (555) 999")
        self.assertTrue(self.bob.share_address)
        self.assertFalse(self.bob.share_phone_number)

    def test_unchecked_html_checkboxes_revoke_sharing(self):
        self.bob.share_address = self.bob.share_phone_number = True
        self.bob.save()
        self.client.force_login(self.bob)
        data = self.data()
        del data["share_address"]
        del data["share_phone_number"]
        self.client.post(reverse("my_profile"), data)
        self.bob.refresh_from_db()
        self.assertFalse(self.bob.share_address)
        self.assertFalse(self.bob.share_phone_number)

    def test_forged_target_and_privileged_fields_ignored(self):
        self.client.force_login(self.alice)
        old_bob = self.data()
        self.client.post(
            reverse("my_profile"),
            self.data(
                self.alice,
                address="Alice new address",
                share_address=True,
                user=self.bob.pk,
                member_id=self.bob.pk,
                pk=self.bob.pk,
                is_staff=True,
                is_superuser=True,
                status="suspended",
                email="attacker@example.com",
            ),
        )
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertEqual(self.alice.address, "Alice new address")
        self.assertFalse(self.alice.is_staff)
        self.assertFalse(self.alice.is_superuser)
        self.assertEqual(self.alice.status, "approved")
        self.assertEqual(self.alice.email, "alice@example.com")
        self.assertEqual(self.alice.observatory_number, "")
        self.assertEqual(self.data(), old_bob)

    def test_profile_cannot_be_targeted_by_query_string(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("my_profile") + f"?user={self.bob.pk}")
        self.assertNotContains(response, self.bob.address)
        self.assertNotContains(response, self.bob.phone_number)

    def test_anonymous_and_pending_members_cannot_access(self):
        for name in ("my_profile", "member_directory"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)
        self.alice.status = "pending"
        self.alice.save()
        self.client.force_login(self.alice)
        for name in ("my_profile", "member_directory"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302)
            self.assertNotIn(self.bob.address.encode(), response.content)
        with self.assertRaises(PermissionDenied):
            profiles.save_profile(self.alice, self.data(self.alice))

    def test_inactive_unapproved_deleted_members_excluded(self):
        for changes in (
            {"is_active": False},
            {"status": "pending"},
            {"status": "suspended"},
        ):
            User.objects.filter(pk=self.bob.pk).update(**changes)
            self.assertFalse(profiles.directory_entries().filter(pk=self.bob.pk).exists())
            User.objects.filter(pk=self.bob.pk).update(status="approved", is_active=True)
        user_admin.delete_member(self.admin, self.bob.pk, self.bob.admin_revision)
        self.assertFalse(profiles.directory_entries().filter(pk=self.bob.pk).exists())

    def test_admin_cannot_change_another_members_sharing_preferences(self):
        for changes in ({"share_address": True}, {"share_phone_number": True}):
            with self.assertRaises(ValidationError):
                user_admin.save_member(
                    self.admin, self.admin_data(self.bob, **changes), self.bob.pk
                )
        with self.assertRaises(ValidationError):
            user_admin.save_member(
                self.admin, self.admin_data(self.bob, email="new@example.com", share_address=True)
            )
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_admin_form_protects_preferences_and_preserves_existing_consent(self):
        self.bob.share_phone_number = True
        self.bob.save()
        self.client.force_login(self.admin)
        form = user_admin.MemberForm(member=self.bob, actor=self.admin)
        self.assertTrue(form.fields["share_address"].disabled)
        self.assertTrue(form.fields["share_phone_number"].disabled)
        self.client.post(
            reverse("member_edit", args=[self.bob.pk]),
            self.admin_data(
                self.bob, address="Updated by admin", share_address=True, share_phone_number=False
            ),
        )
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.address, "Updated by admin")
        self.assertFalse(self.bob.share_address)
        self.assertTrue(self.bob.share_phone_number)

    def test_admin_can_set_own_preferences(self):
        user_admin.save_member(
            self.admin, self.admin_data(self.admin, share_address=True), self.admin.pk
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.share_address)

    def test_stale_admin_and_owner_edits_cannot_overwrite_each_other(self):
        stale_admin = self.admin_data(self.bob)
        profiles.save_profile(self.bob, self.data(share_address=True))
        with self.assertRaises(ValidationError):
            user_admin.save_member(self.admin, stale_admin, self.bob.pk)
        self.bob.refresh_from_db()
        stale_profile = self.data()
        user_admin.save_member(self.admin, self.admin_data(self.bob), self.bob.pk)
        with self.assertRaises(ValidationError):
            profiles.save_profile(self.bob, stale_profile)

    def test_length_limits_enforced(self):
        for key, length in (("address", 2000), ("phone_number", 40)):
            with self.subTest(field=key), self.assertRaises(ValidationError):
                profiles.save_profile(self.bob, self.data(**{key: "x" * (length + 1)}))

    def test_profile_audit_does_not_retain_contact_details(self):
        profiles.save_profile(self.bob, self.data(share_phone_number=True))
        event = AuditEvent.objects.get()
        self.assertEqual(event.action, "profile_updated")
        self.assertNotIn(self.bob.address, str(event.details))
        self.assertNotIn(self.bob.phone_number, str(event.details))
        self.assertTrue(event.details["share_phone_number"])

    def test_profile_audit_failure_rolls_back(self):
        with patch("lodge.profiles.audit", side_effect=RuntimeError("audit offline")):
            with self.assertRaises(RuntimeError):
                profiles.save_profile(self.bob, self.data(share_address=True))
        self.bob.refresh_from_db()
        self.assertFalse(self.bob.share_address)

    def test_csrf_required_for_profile_changes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.bob)
        self.assertEqual(client.post(reverse("my_profile"), self.data()).status_code, 403)

    def test_navigation_available_to_members_and_admins(self):
        for member in (self.alice, self.admin):
            self.client.force_login(member)
            response = self.client.get("/")
            self.assertContains(response, reverse("my_profile"))
            self.assertContains(response, reverse("member_directory"))
