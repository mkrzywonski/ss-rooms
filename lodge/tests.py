import io
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.conf import settings
from django.core import mail, signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import services
from .models import (
    AuditEvent,
    Booking,
    Commitment,
    Consumable,
    Invitation,
    MaintenanceBlock,
    Notification,
    Reservation,
    Resource,
    StockObservation,
    User,
)
from .notifications import deliver_one


class Fixtures:
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user(
            "alice@example.com", "stellar-test-password", name="Alice", status="approved"
        )
        self.bob = User.objects.create_user(
            "bob@example.com", "stellar-test-password", name="Bob", status="approved"
        )
        self.admin = User.objects.create_superuser(
            "admin@example.com", "stellar-test-password", name="Administrator"
        )
        self.room = Resource.objects.create(name="East bedroom", capacity=2, sharing_enabled=True)
        self.second = Resource.objects.create(name="West bedroom")
        self.start = timezone.localdate() + timedelta(days=10)
        self.end = self.start + timedelta(days=3)
        self.item = Consumable.objects.create(name="Toilet paper")

    def row(self, room=None, start=None, end=None, places=1, share=False):
        return {
            "resource": (room or self.room).pk,
            "arrival": start or self.start,
            "departure": end or self.end,
            "places": places,
            "share": share,
        }

    def book(self, actor=None, rows=None, **kwargs):
        return services.save_booking(actor or self.alice, rows or [self.row()], **kwargs)


class BookingTests(Fixtures, TestCase):
    def test_private_first_come(self):
        self.book()
        with self.assertRaises(ValidationError):
            self.book(self.bob)

    def test_adjacent_nights_allowed(self):
        self.book()
        self.book(self.bob, [self.row(start=self.end, end=self.end + timedelta(days=1))])
        self.assertEqual(Booking.objects.count(), 2)

    def test_shared_capacity_and_mutual_consent(self):
        self.book(rows=[self.row(share=True)])
        self.book(self.bob, [self.row(share=True)])
        with self.assertRaises(ValidationError):
            self.book(self.admin, [self.row(share=True)])

    def test_private_reservation_prevents_shared_booking(self):
        self.book()
        with self.assertRaises(ValidationError):
            self.book(self.bob, [self.row(share=True)])

    def test_shared_booking_prevents_private_takeover(self):
        booking = self.book(rows=[self.row(share=True)])
        self.book(self.bob, [self.row(share=True)])
        with self.assertRaises(ValidationError):
            self.book(rows=[self.row()], booking_id=booking.pk, revision=1)
        self.assertTrue(booking.reservations.get(canceled_at__isnull=True).share)

    def test_capacity_is_checked_per_night_not_summed_across_stay(self):
        split = self.start + timedelta(days=1)
        self.book(rows=[self.row(end=split, share=True)])
        self.book(rows=[self.row(start=split, share=True)])
        self.book(self.bob, [self.row(share=True)])
        self.assertEqual(Booking.objects.count(), 3)

    def test_partial_overlap_over_capacity(self):
        self.book(rows=[self.row(share=True)])
        self.book(self.bob, [self.row(start=self.start + timedelta(days=1), share=True)])
        with self.assertRaises(ValidationError):
            self.book(self.admin, [self.row(end=self.start + timedelta(days=2), share=True)])

    def test_capacity_and_disabled_sharing(self):
        with self.assertRaises(ValidationError):
            self.book(rows=[self.row(places=3)])
        with self.assertRaises(ValidationError):
            self.book(rows=[self.row(room=self.second, share=True)])

    def test_multiroom_failure_rolls_back_all(self):
        self.book(self.bob, [self.row(room=self.second)])
        before_mail = Notification.objects.count()
        with self.assertRaises(ValidationError):
            self.book(rows=[self.row(), self.row(room=self.second)])
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), before_mail)
        self.assertFalse(Reservation.objects.filter(resource=self.room).exists())

    def test_failed_change_keeps_original_and_email(self):
        mine = self.book()
        later = self.end + timedelta(days=3)
        self.book(self.bob, [self.row(start=self.end, end=later)])
        with self.assertRaises(ValidationError):
            self.book(rows=[self.row(start=self.end, end=later)], booking_id=mine.pk, revision=1)
        active = mine.reservations.get(canceled_at__isnull=True)
        self.assertEqual(active.arrival, self.start)
        mine.refresh_from_db()
        self.assertEqual(mine.revision, 1)

    def test_stale_edits_fail(self):
        booking = self.book()
        services.save_booking(self.alice, [self.row()], booking_id=booking.pk, revision=1)
        with self.assertRaises(ValidationError):
            services.save_booking(self.alice, [], booking_id=booking.pk, revision=1)

    def test_cancellation_releases_room_and_flags_restock(self):
        booking = self.book()
        plan = Commitment.objects.create(
            item=self.item, member=self.alice, booking=booking, expected_date=self.start
        )
        services.save_booking(self.alice, [], booking_id=booking.pk, revision=1)
        self.book(self.bob)
        plan.refresh_from_db()
        self.assertTrue(plan.needs_review)
        self.assertEqual(plan.status, "planned")
        self.assertFalse(booking.active)

    def test_other_member_cannot_change(self):
        booking = self.book()
        with self.assertRaises(PermissionDenied):
            services.save_booking(self.bob, [], booking_id=booking.pk, revision=1)

    def test_admin_must_explain_changes(self):
        booking = self.book()
        with self.assertRaises(ValidationError):
            services.save_booking(self.admin, [], booking_id=booking.pk, revision=1)
        services.save_booking(
            self.admin, [], booking_id=booking.pk, revision=1, reason="Room unavailable"
        )
        self.assertIn("Room unavailable", Notification.objects.latest("pk").body)

    def test_maintenance_cannot_displace_reservation(self):
        self.book()
        with self.assertRaises(ValidationError):
            services.block_resource(
                self.admin,
                {
                    "resource": self.room,
                    "arrival": self.start,
                    "departure": self.end,
                    "reason": "Plumbing",
                },
            )
        self.assertEqual(MaintenanceBlock.objects.count(), 0)

    def test_block_prevents_reservation_then_can_be_removed(self):
        block = services.block_resource(
            self.admin,
            {
                "resource": self.room,
                "arrival": self.start,
                "departure": self.end,
                "reason": "Plumbing",
            },
        )
        with self.assertRaises(ValidationError):
            self.book()
        services.remove_block(self.admin, block.pk)
        self.book()

    def test_capacity_and_sharing_configuration_preserves_bookings(self):
        self.book(rows=[self.row(share=True)])
        self.book(self.bob, [self.row(share=True)])
        data = {
            "name": self.room.name,
            "description": "",
            "capacity": 1,
            "sharing_enabled": True,
            "active": True,
        }
        with self.assertRaises(ValidationError):
            services.configure_resource(self.admin, self.room.pk, data)
        data.update(capacity=2, sharing_enabled=False)
        with self.assertRaises(ValidationError):
            services.configure_resource(self.admin, self.room.pk, data)
        data.update(sharing_enabled=True, active=False)
        with self.assertRaises(ValidationError):
            services.configure_resource(self.admin, self.room.pk, data)

    def test_no_past_or_zero_length_stay(self):
        for row in [
            self.row(start=timezone.localdate() - timedelta(days=1)),
            self.row(end=self.start),
        ]:
            with self.assertRaises(ValidationError):
                self.book(rows=[row])

    def test_email_and_audit_are_transactional(self):
        with patch("lodge.services.queue_email", side_effect=RuntimeError("queue failure")):
            with self.assertRaises(RuntimeError):
                self.book()
        self.assertFalse(Booking.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())


class ConcurrencyTests(Fixtures, TransactionTestCase):
    def test_simultaneous_private_booking_has_exactly_one_winner(self):
        barrier = Barrier(2)
        row = self.row()

        def attempt(user_id):
            close_old_connections()
            try:
                actor = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                services.save_booking(actor, [row])
                return "confirmed"
            except ValidationError:
                return "conflict"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [self.alice.pk, self.bob.pk]))
        self.assertCountEqual(results, ["confirmed", "conflict"])
        self.assertEqual(Reservation.objects.filter(canceled_at__isnull=True).count(), 1)

    def test_simultaneous_shared_bookings_cannot_exceed_capacity(self):
        self.book(rows=[self.row(share=True)])
        barrier = Barrier(2)

        def attempt(user_id):
            close_old_connections()
            try:
                actor = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                services.save_booking(actor, [self.row(share=True)])
                return True
            except ValidationError:
                return False
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [self.bob.pk, self.admin.pk]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(Reservation.objects.filter(canceled_at__isnull=True).count(), 2)


class MembershipTests(Fixtures, TestCase):
    def registration_data(self):
        return {
            "email": "new@example.com",
            "name": "New Member",
            "password1": "new-stellar-password",
            "connection": "Remote observer",
        }

    def test_registration_verification_then_approval(self):
        applicant = services.register_member(self.registration_data())
        self.assertEqual(applicant.status, "unverified")
        self.assertEqual(Notification.objects.count(), 1)
        token = signing.dumps({"id": applicant.pk, "email": applicant.email}, salt="verify-email")
        services.verify_member(token)
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, "pending")
        self.assertEqual(Notification.objects.filter(recipient=self.admin.email).count(), 1)
        services.verify_member(token)
        self.assertEqual(Notification.objects.filter(recipient=self.admin.email).count(), 1)
        services.decide_member(self.admin, applicant.pk, "approved")
        applicant.refresh_from_db()
        self.assertEqual(applicant.status, "approved")

    def test_unverified_cannot_be_approved(self):
        applicant = services.register_member(self.registration_data())
        with self.assertRaises(ValidationError):
            services.decide_member(self.admin, applicant.pk, "approved")

    def test_bad_verification_token(self):
        with self.assertRaises(ValidationError):
            services.verify_member("bad-token")

    @override_settings(INVITATION_REQUIRED=True)
    def test_invitation_bound_to_email_and_used_once(self):
        data = self.registration_data()
        with self.assertRaises(ValidationError):
            services.register_member(data)
        invitation = Invitation.objects.create(
            email=data["email"], expires_at=timezone.now() + timedelta(days=1)
        )
        data["invitation"] = invitation.token
        applicant = services.register_member(data)
        self.assertIsNotNone(applicant)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.used_at)

    @override_settings(REGISTRATION_ENABLED=False)
    def test_registration_can_be_closed(self):
        with self.assertRaises(ValidationError):
            services.register_member(self.registration_data())

    def test_pending_member_cannot_book_or_see_member_content(self):
        applicant = services.register_member(self.registration_data())
        self.client.force_login(applicant)
        for url in ["/", "/calendar/", "/problems/", "/supplies/", "/bookings/new/"]:
            self.assertRedirects(self.client.get(url), "/account/", fetch_redirect_response=False)
        with self.assertRaises(PermissionDenied):
            self.book(applicant)

    def test_suspension_invalidates_session(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/").status_code, 200)
        services.decide_member(self.admin, self.alice.pk, "suspended")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_member_cannot_approve_or_access_admin(self):
        with self.assertRaises(PermissionDenied):
            services.decide_member(self.alice, self.bob.pk, "suspended")
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/manage/members/").status_code, 403)

    def test_administrator_roles_and_readonly_admin(self):
        services.change_admin_role(self.admin, self.bob.pk, True)
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.is_staff)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get("/manage/members/").status_code, 200)
        self.assertEqual(self.client.get("/admin/lodge/notification/").status_code, 200)
        self.assertEqual(self.client.get("/admin/lodge/reservation/add/").status_code, 403)
        services.change_admin_role(self.admin, self.bob.pk, False)
        self.assertEqual(self.client.get("/manage/members/").status_code, 403)

    def test_registration_honeypot(self):
        response = self.client.post(
            "/register/",
            {**self.registration_data(), "password2": "new-stellar-password", "website": "spam"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to process")
        self.assertFalse(User.objects.filter(email="new@example.com").exists())


class PropertyAndSuppliesTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.media.cleanup)
        self.addCleanup(self.override.disable)

    def problem(self):
        return services.create_problem(
            self.alice,
            {
                "title": "Leaky faucet",
                "description": "Drips",
                "location": "Kitchen",
                "resource": None,
                "urgency": "routine",
            },
            [],
        )

    def test_new_problem_notifies_admin(self):
        problem = self.problem()
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.admin.email, subject__contains="New problem"
            ).exists()
        )
        self.assertEqual(problem.status, "open")

    def test_any_member_can_resolve_and_reopen_with_history(self):
        problem = self.problem()
        with self.assertRaises(ValidationError):
            services.update_problem(self.bob, problem.pk, "resolved", "")
        services.update_problem(self.bob, problem.pk, "resolved", "Replaced washer")
        services.update_problem(self.alice, problem.pk, "open", "Still dripping")
        problem.refresh_from_db()
        self.assertEqual(problem.status, "open")
        self.assertEqual(problem.updates.count(), 2)

    def test_photos_are_reencoded_and_private(self):
        stream = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(stream, "PNG")
        upload = SimpleUploadedFile("photo.png", stream.getvalue(), content_type="image/png")
        problem = services.create_problem(
            self.alice,
            {
                "title": "Window",
                "description": "Broken",
                "location": "East",
                "resource": self.room,
                "urgency": "soon",
            },
            services.prepare_photos([upload]),
        )
        photo = problem.photos.get()
        self.assertTrue(photo.image.name.endswith(".jpg"))
        self.assertEqual(self.client.get(reverse("photo", args=[photo.pk])).status_code, 302)
        self.client.force_login(self.bob)
        response = self.client.get(reverse("photo", args=[photo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(b"".join(response.streaming_content).startswith(bytes.fromhex("ffd8")))

    def test_invalid_photo_rejected_before_creating_report(self):
        with self.assertRaises(ValidationError):
            services.prepare_photos([SimpleUploadedFile("fake.jpg", b"not-an-image")])

    def test_stock_history_and_stale_update(self):
        services.observe_stock(self.alice, self.item.pk, "out", "Last roll used", 1)
        with self.assertRaises(ValidationError):
            services.observe_stock(self.bob, self.item.pk, "plenty", "", 1)
        self.item.refresh_from_db()
        self.assertEqual(self.item.level, "out")
        self.assertEqual(StockObservation.objects.count(), 1)

    def test_planned_delivery_does_not_change_stock(self):
        plan = services.save_commitment(
            self.alice,
            {
                "item": self.item,
                "booking": None,
                "expected_date": self.start,
                "description": "12 rolls",
                "status": "planned",
            },
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.level, "unknown")
        with self.assertRaises(PermissionDenied):
            services.save_commitment(
                self.bob,
                {
                    "item": self.item,
                    "booking": None,
                    "expected_date": self.start,
                    "description": "",
                    "status": "canceled",
                },
                plan.pk,
                1,
            )

    def test_cannot_link_another_members_booking(self):
        booking = self.book(self.bob)
        with self.assertRaises(ValidationError):
            services.save_commitment(
                self.alice,
                {
                    "item": self.item,
                    "booking": booking,
                    "expected_date": self.start,
                    "description": "",
                    "status": "planned",
                },
            )


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MailTests(Fixtures, TestCase):
    def test_delivery_and_no_duplicate_resend(self):
        self.book()
        self.assertTrue(deliver_one())
        self.assertFalse(deliver_one())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNotNone(Notification.objects.get().sent_at)

    def test_mail_failure_retained_for_retry(self):
        self.book()
        with patch("lodge.notifications.send_mail", side_effect=OSError("offline")):
            self.assertTrue(deliver_one())
        message = Notification.objects.get()
        self.assertIsNone(message.sent_at)
        self.assertEqual(message.attempts, 1)
        self.assertEqual(message.last_error, "OSError")
        self.assertGreater(message.next_attempt, timezone.now())
        Notification.objects.filter(pk=message.pk).update(next_attempt=timezone.now())
        self.assertTrue(deliver_one())
        message.refresh_from_db()
        self.assertIsNotNone(message.sent_at)

    def test_expired_worker_lease_is_recovered(self):
        self.book()
        Notification.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(deliver_one())

    def test_password_reset_uses_configured_origin_and_queue(self):
        response = self.client.post(reverse("password_reset"), {"email": self.alice.email})
        self.assertEqual(response.status_code, 302)
        message = Notification.objects.get()
        self.assertIn(settings.PUBLIC_URL + "/accounts/reset/", message.body)
        self.assertEqual(len(getattr(mail, "outbox", [])), 0)


class LoggingTests(TestCase):
    def test_verification_and_reset_tokens_are_redacted(self):
        import logging

        from .logging import RedactTokenPaths

        for path in ("/verify/secret-signature/", "/accounts/reset/uid/secret-token/"):
            record = logging.LogRecord("django", 30, "", 1, "Bad Request: %s", (path,), None)
            RedactTokenPaths().filter(record)
            self.assertNotIn("secret", record.getMessage())
            self.assertIn("[redacted]", record.getMessage())


class PageTests(Fixtures, TestCase):
    def test_new_reservations_cannot_start_in_the_past(self):
        self.client.force_login(self.alice)
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        url = f"/bookings/new/?room={self.room.pk}"
        response = self.client.get(
            f"{url}&arrival={yesterday.isoformat()}&departure={today.isoformat()}"
        )
        form = response.context["form"]
        self.assertEqual(form.initial["arrival"], today)
        self.assertEqual(form.initial["departure"], (today + timedelta(days=1)).isoformat())
        self.assertEqual(form.fields["arrival"].widget.attrs["min"], today.isoformat())
        self.assertEqual(
            form.fields["departure"].widget.attrs["min"], (today + timedelta(days=1)).isoformat()
        )
        data = {
            "arrival": yesterday.isoformat(),
            "departure": (today + timedelta(days=1)).isoformat(),
            "party_size": 1,
            f"places_{self.room.pk}": 1,
        }
        response = self.client.post(url, data)
        self.assertContains(response, "The first night cannot be in the past.")
        self.assertFalse(Booking.objects.exists())
        data["arrival"] = today.isoformat()
        self.assertEqual(self.client.post(url, data).status_code, 302)

    def test_past_calendar_nights_have_no_booking_links(self):
        self.client.force_login(self.alice)
        past = timezone.localdate() - timedelta(days=40)
        booking = self.book(rows=[self.row(share=True)])
        booking.reservations.update(arrival=past, departure=past + timedelta(days=1))
        for view in ("month", "week"):
            response = self.client.get(f"/calendar/?date={past.isoformat()}&view={view}")
            for day in response.context["days"]:
                for cell in day["cells"]:
                    self.assertFalse(cell["bookable"])
                    self.assertEqual(cell["state"], "past")
                    self.assertEqual(cell["label"], "Past")
            self.assertNotContains(response, "Book this night")
            self.assertNotContains(response, 'class="available"')
            self.assertNotContains(response, 'class="shared"')
            self.assertContains(response, reverse("booking_detail", args=[booking.pk]))
        response = self.client.get("/calendar/")
        today = timezone.localdate()
        for day in response.context["days"]:
            if day["date"] >= today:
                self.assertTrue(all(cell["state"] == "available" for cell in day["cells"]))

    def test_supplies_has_no_bring_button(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("supplies"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{reverse("commitment_new")}"')
        self.assertContains(response, "Restocking plans")

    def test_member_pages_render(self):
        booking = self.book()
        problem = services.create_problem(
            self.alice,
            {
                "title": "Faucet",
                "description": "Drip",
                "location": "Kitchen",
                "resource": None,
                "urgency": "routine",
            },
            [],
        )
        self.client.force_login(self.alice)
        for url in [
            "/",
            "/calendar/",
            "/calendar/?view=week",
            "/calendar/?view=list",
            "/bookings/new/",
            f"/bookings/{booking.pk}/",
            f"/bookings/{booking.pk}/edit/",
            f"/reservations/{booking.reservations.first().pk}/edit/",
            "/problems/",
            "/problems/new/",
            f"/problems/{problem.pk}/",
            "/supplies/",
            f"/supplies/{self.item.pk}/stock/",
            "/restocking/new/",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_administration_pages_render(self):
        self.client.force_login(self.admin)
        for url in [
            "/manage/members/",
            "/manage/bedrooms/",
            "/manage/bedrooms/new/",
            f"/manage/bedrooms/{self.room.pk}/",
            "/manage/blocks/new/",
            "/manage/supplies/new/",
            f"/manage/supplies/{self.item.pk}/",
            "/admin/",
            "/admin/lodge/notification/",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_calendar_date_prefills_a_valid_overnight_stay(self):
        self.client.force_login(self.alice)
        response = self.client.get(f"/bookings/new/?arrival={self.start.isoformat()}")
        form = response.context["form"]
        self.assertEqual(form.initial["arrival"], self.start)
        self.assertEqual(form.initial["departure"], (self.start + timedelta(days=1)).isoformat())

    def test_individual_room_cancellation_preserves_other_rooms(self):
        booking = self.book(rows=[self.row(), self.row(room=self.second)])
        self.client.force_login(self.alice)
        response = self.client.post(
            reverse("booking_cancel", args=[booking.pk]),
            {"revision": 1, "resource": self.room.pk},
        )
        self.assertEqual(response.status_code, 302)
        remaining = booking.reservations.get(canceled_at__isnull=True)
        self.assertEqual(remaining.resource_id, self.second.pk)
        self.assertEqual(remaining.arrival, self.start)

    def test_browser_booking_submission(self):
        self.client.force_login(self.alice)
        response = self.client.post(
            f"/bookings/new/?room={self.room.pk}",
            {
                "arrival": self.start.isoformat(),
                "departure": self.end.isoformat(),
                "party_size": 1,
                f"places_{self.room.pk}": 1,
                f"places_{self.second.pk}": 0,
                "notes": "Arriving after midnight",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Booking.objects.get().notes, "Arriving after midnight")

    def test_room_specific_form_and_calendar_links(self):
        self.client.force_login(self.alice)
        response = self.client.get(f"/bookings/new/?room={self.room.pk}")
        self.assertContains(response, f'name="places_{self.room.pk}"')
        self.assertNotContains(response, f'name="places_{self.second.pk}"')
        self.assertNotContains(response, f'name="share_{self.second.pk}"')
        response = self.client.get("/calendar/")
        self.assertContains(response, f"?room={self.room.pk}&amp;arrival=")
        response = self.client.get("/bookings/new/")
        self.assertContains(response, "Choose a bedroom")

    def test_sharing_checkbox_only_for_enabled_rooms(self):
        self.client.force_login(self.alice)
        for room in (self.room, self.second):
            with self.subTest(room=room.name):
                response = self.client.get(f"/bookings/new/?room={room.pk}")
                checkbox = f'name="share_{room.pk}"'
                if room.sharing_enabled:
                    self.assertContains(response, checkbox)
                else:
                    self.assertNotContains(response, checkbox)
                data = {
                    "arrival": self.start.isoformat(),
                    "departure": self.end.isoformat(),
                    "party_size": 1,
                    f"places_{room.pk}": 1,
                    f"share_{room.pk}": "on",
                }
                response = self.client.post(f"/bookings/new/?room={room.pk}", data)
                self.assertEqual(response.status_code, 302)
                reservation = Reservation.objects.get(resource=room, canceled_at__isnull=True)
                self.assertEqual(reservation.share, room.sharing_enabled)
                response = self.client.get(reverse("booking_edit", args=[reservation.booking_id]))
                if room.sharing_enabled:
                    self.assertContains(response, checkbox)
                else:
                    self.assertNotContains(response, checkbox)
                url = reverse("reservation_edit", args=[reservation.pk])
                response = self.client.get(url)
                if room.sharing_enabled:
                    self.assertContains(response, 'name="share"')
                else:
                    self.assertNotContains(response, 'name="share"')
                response = self.client.post(
                    url,
                    {
                        "arrival": self.start.isoformat(),
                        "departure": self.end.isoformat(),
                        "places": 1,
                        "share": "on",
                        "revision": reservation.booking.revision,
                    },
                )
                self.assertEqual(response.status_code, 302)
                replacement = Reservation.objects.get(resource=room, canceled_at__isnull=True)
                self.assertEqual(replacement.share, room.sharing_enabled)

    def test_new_booking_only_saves_selected_room(self):
        self.client.force_login(self.alice)
        data = {
            "arrival": self.start.isoformat(),
            "departure": self.end.isoformat(),
            "party_size": 1,
            f"places_{self.room.pk}": 1,
            f"places_{self.second.pk}": 1,
        }
        response = self.client.post(f"/bookings/new/?room={self.room.pk}", data)
        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get()
        self.assertEqual(
            list(booking.reservations.values_list("resource_id", flat=True)), [self.room.pk]
        )
        response = self.client.get(reverse("booking_edit", args=[booking.pk]))
        self.assertContains(response, f'name="places_{self.room.pk}"')
        self.assertNotContains(response, f'name="places_{self.second.pk}"')
        data["revision"] = booking.revision
        self.assertEqual(
            self.client.post(reverse("booking_edit", args=[booking.pk]), data).status_code, 302
        )
        self.assertEqual(booking.reservations.filter(canceled_at__isnull=True).count(), 1)

    def test_room_selection_required_and_invalid_rooms_rejected(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.post("/bookings/new/", {}).status_code, 400)
        for room in ("invalid", "9999999999999999999999999999999"):
            self.assertEqual(self.client.get(f"/bookings/new/?room={room}").status_code, 404)
        self.second.active = False
        self.second.save()
        self.assertEqual(self.client.get(f"/bookings/new/?room={self.second.pk}").status_code, 404)
        self.assertFalse(Booking.objects.exists())

    def test_booking_change_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post("/bookings/new/", {})
        self.assertEqual(response.status_code, 403)

    def test_auth_rate_limit(self):
        for _ in range(8):
            self.client.post("/register/", {})
        self.assertEqual(self.client.post("/register/", {}).status_code, 429)
