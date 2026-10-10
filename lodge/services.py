import io
import uuid
import warnings

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import (
    AuditEvent,
    Booking,
    Commitment,
    Consumable,
    Invitation,
    MaintenanceBlock,
    Notification,
    Problem,
    ProblemPhoto,
    ProblemSubscription,
    ProblemUpdate,
    Reservation,
    Resource,
    StockObservation,
    User,
)


def require_member(actor, admin=False):
    users = (
        User.objects.select_for_update(no_key=True) if connection.in_atomic_block else User.objects
    )
    current = users.get(pk=actor.pk)
    if not current.is_active or current.status != "approved" or (admin and not current.is_staff):
        raise PermissionDenied


def lock_user_admin(actor):
    # Serialize account administration; member actions lock their own user row.
    # Both use NO KEY UPDATE row locks so historical foreign keys remain usable.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(19810428, 1)")
    require_member(actor, admin=True)
    return User.objects.get(pk=actor.pk)


def audit(actor, action, obj, **details):
    AuditEvent.objects.create(
        actor=actor, action=action, target=f"{obj._meta.label}:{obj.pk}", details=details
    )


def queue_email(recipients, subject, body):
    for email in sorted(set(recipients)):
        Notification.objects.create(
            recipient=email, subject=f"[Stellar Skies Lodge] {subject}", body=body
        )


def admin_emails():
    return list(
        User.objects.filter(is_staff=True, is_active=True, status="approved").values_list(
            "email", flat=True
        )
    )


def absolute(path):
    return settings.PUBLIC_URL + path


def queue_verification(user):
    token = signing.dumps({"id": user.pk, "email": user.email}, salt="verify-email")
    queue_email(
        [user.email],
        "Verify your email",
        f"Confirm your email address to request membership approval:\n\n{absolute(reverse('verify', args=[token]))}\n\nThis link expires in 48 hours. Verification does not grant booking access; an administrator must approve your account.",
    )


@transaction.atomic
def register_member(data):
    if not settings.REGISTRATION_ENABLED:
        raise ValidationError("Registration is currently closed. Please contact an administrator.")
    invite = None
    if settings.INVITATION_REQUIRED:
        try:
            invite = Invitation.objects.select_for_update().get(
                token=data.get("invitation"),
                email__iexact=data["email"],
                used_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
        except (Invitation.DoesNotExist, ValidationError, ValueError):
            raise ValidationError("A valid invitation for this email address is required.")
    if User.objects.filter(email__iexact=data["email"]).exists():
        return None  # Same outward response to avoid disclosing registered email addresses.
    user = User.objects.create_user(
        data["email"], data["password1"], name=data["name"], connection=data["connection"]
    )
    if invite:
        invite.used_at = timezone.now()
        invite.save(update_fields=["used_at"])
    queue_verification(user)
    audit(user, "registration_requested", user)
    return user


@transaction.atomic
def verify_member(token):
    try:
        data = signing.loads(token, salt="verify-email", max_age=48 * 3600)
        user = User.objects.select_for_update().get(pk=data["id"], email=data["email"])
    except (signing.BadSignature, User.DoesNotExist, KeyError):
        raise ValidationError(
            "This verification link is invalid or expired. Sign in to request another."
        )
    if user.deleted_at or not user.is_active:
        raise ValidationError("This account is no longer active.")
    if user.status == "unverified":
        user.status = "pending"
        user.admin_revision += 1
        user.save(update_fields=["status", "admin_revision"])
        audit(user, "email_verified", user)
        queue_email(
            admin_emails(),
            "Membership approval requested",
            f"{user.name} ({user.email}) has verified their email.\n\nConnection to Stellar Skies: {user.connection}\n\nReview: {absolute(reverse('members'))}",
        )
    return user


@transaction.atomic
def decide_member(actor, user_id, decision, reason=""):
    actor = lock_user_admin(actor)
    user = User.objects.select_for_update(no_key=True).get(pk=user_id)
    if decision not in ("approved", "rejected", "suspended"):
        raise ValidationError("Invalid membership decision.")
    if user.pk == actor.pk:
        raise ValidationError("You cannot change your own membership status here.")
    if user.is_staff and decision != "approved":
        raise ValidationError(
            "Remove administrator privileges before suspending or rejecting this account."
        )
    if decision in ("approved", "rejected") and user.status not in ("pending", "suspended"):
        raise ValidationError("Only verified pending or suspended accounts can be reviewed.")
    if decision == "suspended" and user.status != "approved":
        raise ValidationError("Only approved accounts can be suspended.")
    if user.deleted_at:
        raise ValidationError("This account has been deleted.")
    user.status = decision
    user.admin_revision += 1
    user.save(update_fields=["status", "admin_revision"])
    audit(actor, "membership_" + decision, user, reason=reason)
    queue_email(
        [user.email],
        f"Membership {decision}",
        f"Your Lodge account is now {decision}.\n\n{reason}\n\n{absolute(reverse('login'))}",
    )


@transaction.atomic
def change_admin_role(actor, user_id, enabled):
    actor = lock_user_admin(actor)
    user = User.objects.select_for_update(no_key=True).get(pk=user_id)
    if user.pk == actor.pk:
        raise ValidationError("Ask another administrator to change your role.")
    if user.is_superuser:
        raise ValidationError("Superuser roles must be managed from the command line.")
    if user.status != "approved" or not user.is_active:
        raise ValidationError("Only active, approved members can be administrators.")
    user.is_staff = enabled
    user.admin_revision += 1
    user.save(update_fields=["is_staff", "admin_revision"])
    audit(actor, "administrator_granted" if enabled else "administrator_removed", user)
    queue_email(
        [user.email],
        "Administrator role updated",
        f"Your Lodge administrator access is now {'enabled' if enabled else 'disabled'}.\n\n{absolute(reverse('dashboard'))}",
    )


def validate_occupancy(resource, arrival, departure, places, share, exclude_booking=None):
    if departure <= arrival:
        raise ValidationError("Departure must be after the first night.")
    if places < 1 or places > resource.capacity:
        raise ValidationError(f"{resource.name} has {resource.capacity} sleeping place(s).")
    if not resource.active:
        raise ValidationError(f"{resource.name} is inactive.")
    if share and not resource.sharing_enabled:
        raise ValidationError(f"{resource.name} does not currently support sharing.")
    if MaintenanceBlock.objects.filter(
        resource=resource, arrival__lt=departure, departure__gt=arrival
    ).exists():
        raise ValidationError(f"{resource.name} is blocked for maintenance during this stay.")
    others = Reservation.objects.filter(
        resource=resource, canceled_at__isnull=True, arrival__lt=departure, departure__gt=arrival
    )
    if exclude_booking:
        others = others.exclude(booking_id=exclude_booking)
    others = list(others)
    if others and (not share or any(not r.share for r in others)):
        raise ValidationError(
            f"{resource.name} is already reserved without mutual sharing consent."
        )
    # Evaluate the occupancy at each date where it can change, not the sum of all
    # reservations that intersect the entire requested stay.
    boundaries = {arrival} | {r.arrival for r in others if arrival < r.arrival < departure}
    for day in boundaries:
        occupied = sum(r.places for r in others if r.arrival <= day < r.departure)
        if occupied + places > resource.capacity:
            raise ValidationError(f"{resource.name} has insufficient shared space on {day}.")


def booking_rows(booking):
    return [
        {
            "resource": r.resource_id,
            "arrival": r.arrival,
            "departure": r.departure,
            "places": r.places,
            "share": r.share,
        }
        for r in booking.reservations.filter(canceled_at__isnull=True)
    ]


@transaction.atomic
def save_booking(
    actor,
    rows,
    *,
    booking_id=None,
    revision=None,
    party_size=1,
    guest_names="",
    notes="",
    reason="",
):
    require_member(actor)
    booking = None
    old_ids = []
    before = []
    if booking_id:
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        if actor.pk != booking.owner_id and not actor.is_staff:
            raise PermissionDenied
        if actor.pk != booking.owner_id and not reason.strip():
            raise ValidationError("Give a reason for changing another member's booking.")
        if booking.revision != revision:
            raise ValidationError(
                "This booking changed while your form was open. Reload it before saving."
            )
        before = booking_rows(booking)
        if not before:
            raise ValidationError("A canceled booking cannot be reactivated. Create a new booking.")
        old_ids = [r["resource"] for r in before]
    ids = [int(r["resource"]) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValidationError("Select each bedroom only once.")
    resources = {
        r.pk: r
        for r in Resource.objects.select_for_update()
        .filter(pk__in=sorted(set(ids + old_ids)))
        .order_by("pk")
    }
    if any(i not in resources for i in ids):
        raise ValidationError("A selected bedroom no longer exists.")
    for row in rows:
        if row["arrival"] < timezone.localdate():
            # Preserve historical rows when canceling or modifying other rooms.
            unchanged = any(row == previous for previous in before)
            if not unchanged:
                raise ValidationError("The first night cannot be in the past.")
        validate_occupancy(
            resources[int(row["resource"])],
            row["arrival"],
            row["departure"],
            row["places"],
            row["share"],
            booking_id,
        )
    if party_size < 1:
        raise ValidationError("Party size must be at least one.")
    if not booking and not rows:
        raise ValidationError("Select at least one bedroom.")
    created = booking is None
    if created:
        booking = Booking.objects.create(owner=actor)
    else:
        booking.reservations.filter(canceled_at__isnull=True).update(canceled_at=timezone.now())
        booking.revision += 1
    booking.party_size, booking.guest_names, booking.notes = party_size, guest_names, notes
    booking.save()
    for row in rows:
        Reservation.objects.create(
            booking=booking,
            resource_id=row["resource"],
            arrival=row["arrival"],
            departure=row["departure"],
            places=row["places"],
            share=row["share"],
        )
    if not created and before != rows:
        Commitment.objects.filter(booking=booking, status="planned").update(needs_review=True)
    action = "created" if created else ("changed" if rows else "canceled")
    audit(
        actor,
        "booking_" + action,
        booking,
        before=[{k: str(v) for k, v in r.items()} for r in before],
        after=[{k: str(v) for k, v in r.items()} for r in rows],
        reason=reason,
    )
    summary = (
        "\n".join(
            f"{resources[int(r['resource'])].name}: {r['arrival']} to {r['departure']} ({r['places']} place(s), {'sharing' if r['share'] else 'private'})"
            for r in rows
        )
        or "All rooms canceled."
    )
    queue_email(
        [booking.owner.email],
        f"Booking #{booking.pk} {action}",
        f"{summary}\n\nParty size: {party_size}\nGuests: {guest_names}\nNotes: {notes}\nChanged by: {actor.name}\nReason: {reason}\n\n{absolute(reverse('booking_detail', args=[booking.pk]))}",
    )
    return booking


@transaction.atomic
def configure_resource(actor, resource_id, data):
    require_member(actor, admin=True)
    resource = (
        Resource.objects.select_for_update().get(pk=resource_id) if resource_id else Resource()
    )
    future = (
        list(
            Reservation.objects.filter(
                resource=resource, canceled_at__isnull=True, departure__gt=timezone.localdate()
            )
        )
        if resource_id
        else []
    )
    if future:
        if not data["active"]:
            raise ValidationError("Resolve upcoming reservations before deactivating this bedroom.")
        if not data["sharing_enabled"] and any(r.share for r in future):
            raise ValidationError("Resolve upcoming shared reservations before disabling sharing.")
        for day in {max(r.arrival, timezone.localdate()) for r in future}:
            if sum(r.places for r in future if r.arrival <= day < r.departure) > data["capacity"]:
                raise ValidationError("The new capacity would invalidate an existing reservation.")
    for key in ("name", "description", "capacity", "sharing_enabled", "active"):
        setattr(resource, key, data[key])
    resource.full_clean()
    resource.save()
    audit(actor, "bedroom_configured", resource)
    return resource


@transaction.atomic
def block_resource(actor, data):
    require_member(actor, admin=True)
    resource = Resource.objects.select_for_update().get(pk=data["resource"].pk)
    if data["departure"] <= data["arrival"]:
        raise ValidationError("Departure must follow the first night.")
    if Reservation.objects.filter(
        resource=resource,
        canceled_at__isnull=True,
        arrival__lt=data["departure"],
        departure__gt=data["arrival"],
    ).exists():
        raise ValidationError(
            "This block conflicts with reservations. Rearrange or cancel them first."
        )
    block = MaintenanceBlock.objects.create(
        resource=resource,
        arrival=data["arrival"],
        departure=data["departure"],
        reason=data["reason"],
        creator=actor,
    )
    audit(actor, "maintenance_block_created", block)
    return block


@transaction.atomic
def remove_block(actor, block_id):
    require_member(actor, admin=True)
    block = MaintenanceBlock.objects.get(pk=block_id)
    Resource.objects.select_for_update().get(pk=block.resource_id)
    audit(actor, "maintenance_block_removed", block, reason=block.reason)
    block.delete()


def prepare_photos(files):
    if len(files) > 5:
        raise ValidationError("Upload no more than five photos at a time.")
    result = []
    for upload in files:
        if upload.size > 10 * 1024 * 1024:
            raise ValidationError("Each photo must be 10 MB or smaller.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload) as source:
                    if source.width * source.height > 25_000_000:
                        raise ValidationError(
                            "Photo dimensions are too large (maximum 25 megapixels)."
                        )
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.thumbnail((1800, 1800))
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=85)  # Re-encoding omits EXIF metadata.
                    result.append(ContentFile(output.getvalue(), name=f"{uuid.uuid4().hex}.jpg"))
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise ValidationError("Upload valid JPEG, PNG, or other supported image files.")
    return result


@transaction.atomic
def create_problem(actor, data, photos):
    require_member(actor)
    problem = Problem.objects.create(reporter=actor, **data)
    for photo in photos:
        ProblemPhoto.objects.create(problem=problem, author=actor, image=photo)
    ProblemSubscription.objects.create(problem=problem, user=actor)
    audit(actor, "problem_reported", problem)
    queue_email(
        admin_emails() + [actor.email],
        f"New problem: {problem.title}",
        f"Reported by {actor.name}\nLocation: {problem.location}\nUrgency: {problem.urgency}\n\n{problem.description}\n\n{absolute(reverse('problem_detail', args=[problem.pk]))}",
    )
    return problem


@transaction.atomic
def update_problem(actor, problem_id, status, comment, claim=False, photos=()):
    require_member(actor)
    problem = Problem.objects.select_for_update().get(pk=problem_id)
    if status not in Problem.Status.values:
        raise ValidationError("Invalid problem status.")
    if status != problem.status and status in ("resolved", "closed") and not comment.strip():
        raise ValidationError("Explain what was done before resolving or closing the problem.")
    if not comment.strip() and status == problem.status and not claim and not photos:
        raise ValidationError("Add a comment, photo, or status change.")
    if problem.photos.count() + len(photos) > 20:
        raise ValidationError("A report may have no more than twenty photos.")
    old_status = problem.status
    ProblemUpdate.objects.create(
        problem=problem, author=actor, comment=comment, from_status=old_status, to_status=status
    )
    problem.status = status
    if claim:
        problem.working_on = actor
    if status in ("resolved", "closed"):
        problem.working_on = None
    problem.save()
    for photo in photos:
        ProblemPhoto.objects.create(problem=problem, author=actor, image=photo)
    ProblemSubscription.objects.get_or_create(problem=problem, user=actor)
    recipients = list(
        problem.subscriptions.filter(
            enabled=True, user__status="approved", user__is_active=True
        ).values_list("user__email", flat=True)
    )
    if status != old_status and (
        status == "resolved"
        or (old_status in ("resolved", "closed") and status in ("open", "progress"))
    ):
        recipients += admin_emails()
    queue_email(
        recipients,
        f"Problem updated: {problem.title}",
        f"{actor.name}: {comment}\nStatus: {old_status} → {status}\n\n{absolute(reverse('problem_detail', args=[problem.pk]))}\nManage email notifications on the report page.",
    )
    audit(actor, "problem_updated", problem, from_status=old_status, to_status=status)
    return problem


@transaction.atomic
def observe_stock(actor, item_id, level, note, revision):
    require_member(actor)
    item = Consumable.objects.select_for_update().get(pk=item_id)
    if not item.active or level not in Consumable.Level.values:
        raise ValidationError("Invalid item or stock level.")
    if item.revision != revision:
        raise ValidationError("Someone updated this item. Reload to see its current stock level.")
    item.level, item.updated_by, item.updated_at = level, actor, timezone.now()
    item.revision += 1
    item.save()
    StockObservation.objects.create(item=item, author=actor, level=level, note=note)
    audit(actor, "stock_observed", item, level=level, note=note)
    return item


@transaction.atomic
def save_commitment(actor, data, commitment_id=None, revision=None):
    require_member(actor)
    if data.get("booking"):
        booking = Booking.objects.select_for_update().get(pk=data["booking"].pk)
        if booking.owner_id != actor.pk or not booking.active:
            raise ValidationError("Link only your own active booking.")
    if not data["item"].active:
        raise ValidationError("This supply item is inactive.")
    if commitment_id:
        item = Commitment.objects.select_for_update().get(pk=commitment_id)
        if actor.pk != item.member_id:
            raise PermissionDenied
        if item.revision != revision:
            raise ValidationError("This commitment changed. Reload before saving.")
        item.revision += 1
    else:
        item = Commitment(member=actor)
    for key in ("item", "booking", "expected_date", "description", "status"):
        setattr(item, key, data[key])
    item.needs_review = False
    item.save()
    audit(actor, "restocking_" + item.status, item)
    return item
