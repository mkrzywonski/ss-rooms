from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from . import email_config, forms, profiles, services, user_admin
from .models import (
    AuditEvent,
    Booking,
    Commitment,
    Consumable,
    MaintenanceBlock,
    Notification,
    Problem,
    ProblemPhoto,
    ProblemSubscription,
    Reservation,
    Resource,
    User,
)


def member_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.user.status != "approved" or not request.user.is_active:
            return redirect("account_status")
        return view(request, *args, **kwargs)

    return wrapped


def admin_required(view):
    @member_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def form_page(request, form, title, **context):
    return render(request, "lodge/form.html", {"form": form, "title": title, **context})


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def register(request):
    if not settings.REGISTRATION_ENABLED:
        return render(
            request,
            "lodge/message.html",
            {
                "title": "Registration closed",
                "text": "Please contact a Stellar Skies administrator for access.",
            },
        )
    form = forms.RegisterForm(
        request.POST or None, initial={"invitation": request.GET.get("invitation", "")}
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.register_member(form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        except IntegrityError:
            # Concurrent duplicate requests get the same outward response.
            return render(
                request,
                "lodge/message.html",
                {
                    "title": "Check your email",
                    "text": "If this address is eligible, a verification link has been sent. Existing members can sign in or reset their password.",
                },
            )
        else:
            return render(
                request,
                "lodge/message.html",
                {
                    "title": "Check your email",
                    "text": "If this address is eligible, a verification link has been sent. After verification, an administrator will review your membership.",
                },
            )
    return form_page(
        request,
        form,
        "Join the Lodge",
        subtitle="For Stellar Skies members. Email verification and administrator approval are required.",
    )


def verify(request, token):
    try:
        services.verify_member(token)
    except ValidationError as error:
        return render(
            request,
            "lodge/message.html",
            {"title": "Unable to verify email", "text": " ".join(error.messages)},
            status=400,
        )
    return render(
        request,
        "lodge/message.html",
        {
            "title": "Email verified",
            "text": "Your account is awaiting administrator approval. We will email you when your registration is complete.",
        },
    )


@login_required
def account_status(request):
    if request.user.status == "approved":
        return redirect("dashboard")
    return render(request, "lodge/status.html")


@login_required
@require_POST
def resend(request):
    if request.user.status == "unverified" and request.user.is_active:
        services.queue_verification(request.user)
        messages.success(request, "Verification email queued.")
    return redirect("account_status")


@member_required
def dashboard(request):
    today = timezone.localdate()
    return render(
        request,
        "lodge/dashboard.html",
        {
            "stays": Reservation.objects.filter(
                canceled_at__isnull=True, departure__gt=today
            ).select_related("booking__owner", "resource")[:12],
            "problems": Problem.objects.exclude(status__in=["resolved", "closed"])[:5],
            "supplies": Consumable.objects.filter(active=True, level__in=["low", "out"]),
            "commitments": Commitment.objects.filter(status="planned").select_related(
                "item", "member"
            )[:10],
            "today": today,
        },
    )


@member_required
def calendar(request):
    today = timezone.localdate()
    try:
        selected = date.fromisoformat(request.GET.get("date", today.isoformat()))
    except ValueError:
        selected = today
    # Keep navigation within a range datetime can safely advance through.
    if selected.year < 1901 or selected.year > 9998:
        selected = today
    view = request.GET.get("view", "month")
    if view == "week":
        start = selected - timedelta(days=selected.weekday())
        end = start + timedelta(days=7)
        previous, following = start - timedelta(days=7), end
    else:
        start = selected.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        previous, following = start - timedelta(days=1), end
    resources = list(Resource.objects.filter(active=True))
    stays = list(
        Reservation.objects.filter(
            canceled_at__isnull=True, arrival__lt=end, departure__gt=start
        ).select_related("booking__owner", "resource")
    )
    blocks = list(MaintenanceBlock.objects.filter(arrival__lt=end, departure__gt=start))
    days = []
    for offset in range((end - start).days):
        day = start + timedelta(days=offset)
        cells = []
        for resource in resources:
            occupants = [
                r for r in stays if r.resource_id == resource.pk and r.arrival <= day < r.departure
            ]
            blocked = next(
                (
                    b
                    for b in blocks
                    if b.resource_id == resource.pk and b.arrival <= day < b.departure
                ),
                None,
            )
            remaining = resource.capacity - sum(r.places for r in occupants)
            if day < today:
                state, label = "past", "Past"
            elif blocked:
                state, label = "blocked", "Maintenance"
            elif not occupants:
                state, label = "available", "Available"
            elif resource.sharing_enabled and all(r.share for r in occupants) and remaining > 0:
                state, label = "shared", f"{remaining} shared place(s) available"
            else:
                state, label = "full", "Full / private"
            cells.append(
                {
                    "room": resource,
                    "stays": occupants,
                    "block": blocked,
                    "state": state,
                    "label": label,
                    "bookable": day >= today and state in ("available", "shared"),
                }
            )
        days.append({"date": day, "departure": day + timedelta(days=1), "cells": cells})
    return render(
        request,
        "lodge/calendar.html",
        {
            "resources": resources,
            "days": days,
            "stays": stays,
            "selected": selected,
            "start": start,
            "end": end,
            "previous": previous,
            "following": following,
            "view": view,
            "timezone": settings.TIME_ZONE,
        },
    )


@member_required
def booking_detail(request, pk, admin_mode=False):
    booking = get_object_or_404(Booking.objects.select_related("owner"), pk=pk)
    return render(
        request,
        "lodge/booking.html",
        {
            "booking": booking,
            "rows": booking.reservations.select_related("resource"),
            "can_edit": request.user.pk == booking.owner_id
            or (admin_mode and request.user.is_staff),
            "admin_mode": admin_mode,
            "events": AuditEvent.objects.filter(target=f"lodge.Booking:{pk}"),
        },
    )


@member_required
def booking_edit(request, pk=None):
    booking = get_object_or_404(Booking, pk=pk) if pk else None
    if booking and booking.owner_id != request.user.pk and not request.user.is_staff:
        raise PermissionDenied
    try:
        arrival = max(
            date.fromisoformat(request.GET.get("arrival", timezone.localdate().isoformat())),
            timezone.localdate(),
        )
        departure = arrival + timedelta(days=1)
    except (ValueError, OverflowError):
        arrival = timezone.localdate()
        departure = arrival + timedelta(days=1)
    try:
        departure = max(
            date.fromisoformat(request.GET.get("departure", departure.isoformat())), departure
        )
    except ValueError:
        pass
    initial = {"arrival": arrival, "departure": departure.isoformat()}
    room = None
    if not booking:
        room_id = request.GET.get("room")
        if room_id:
            if not room_id.isascii() or not room_id.isdecimal() or len(room_id) > 18:
                raise Http404
            room = get_object_or_404(Resource, pk=room_id, active=True)
        else:
            return render(
                request,
                "lodge/select_room.html",
                {
                    "rooms": Resource.objects.filter(active=True),
                    "form": forms.DateForm(initial=initial),
                    "arrival": initial["arrival"],
                    "departure": initial["departure"],
                },
                status=400 if request.method == "POST" else 200,
            )
    form = forms.BookingForm(request.POST or None, booking=booking, room=room, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            result = services.save_booking(
                request.user,
                form.rows(),
                booking_id=pk,
                revision=form.cleaned_data.get("revision"),
                party_size=form.cleaned_data["party_size"],
                guest_names=form.cleaned_data["guest_names"],
                notes=form.cleaned_data["notes"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Booking saved. A confirmation email is queued.")
            return redirect("booking_detail", pk=result.pk)
    return form_page(
        request,
        form,
        "Change booking" if booking else f"Reserve {room.name}",
        subtitle="Reserve one bedroom at a time. For another bedroom, make a separate reservation. Dates refer to nights at the site; after-midnight arrivals may need the previous night.",
    )


@member_required
def reservation_edit(request, pk):
    reservation = get_object_or_404(
        Reservation.objects.select_related("booking", "resource"), pk=pk, canceled_at__isnull=True
    )
    booking = reservation.booking
    if request.user.pk != booking.owner_id and not request.user.is_staff:
        raise PermissionDenied
    form = forms.ReservationForm(
        request.POST or None,
        initial={
            "arrival": reservation.arrival,
            "departure": reservation.departure,
            "places": reservation.places,
            "share": reservation.share,
            "revision": booking.revision,
        },
    )
    form.fields["places"].max_value = reservation.resource.capacity
    if not reservation.resource.sharing_enabled:
        form.fields.pop("share")
    if request.method == "POST" and form.is_valid():
        rows = services.booking_rows(booking)
        for row in rows:
            if row["resource"] == reservation.resource_id:
                row.update(
                    {
                        key: form.cleaned_data.get(key, False)
                        for key in ("arrival", "departure", "places", "share")
                    }
                )
        try:
            services.save_booking(
                request.user,
                rows,
                booking_id=booking.pk,
                revision=form.cleaned_data["revision"],
                party_size=booking.party_size,
                guest_names=booking.guest_names,
                notes=booking.notes,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("booking_detail", pk=booking.pk)
    return form_page(request, form, f"Change {reservation.resource.name}")


@member_required
@require_POST
def booking_cancel(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    rows = services.booking_rows(booking)
    room_id = request.POST.get("resource")
    if room_id:
        rows = [r for r in rows if str(r["resource"]) != room_id]
    else:
        rows = []
    try:
        services.save_booking(
            request.user,
            rows,
            booking_id=pk,
            revision=int(request.POST.get("revision", 0)),
            party_size=booking.party_size,
            guest_names=booking.guest_names,
            notes=booking.notes,
            reason=request.POST.get("reason", ""),
        )
    except (ValidationError, ValueError) as error:
        messages.error(
            request,
            " ".join(error.messages)
            if isinstance(error, ValidationError)
            else "Invalid revision. Reload the booking.",
        )
    else:
        messages.success(request, "Reservation canceled. Check any linked restocking commitments.")
    return redirect("booking_detail", pk=pk)


@member_required
def problems(request):
    query = Problem.objects.select_related("reporter", "working_on")
    if request.GET.get("all") != "1":
        query = query.exclude(status__in=["resolved", "closed"])
    return render(request, "lodge/problems.html", {"problems": query})


@member_required
def problem_new(request):
    form = forms.ProblemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            photos = services.prepare_photos(request.FILES.getlist("photos"))
            problem = services.create_problem(request.user, form.cleaned_data, photos)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("problem_detail", pk=problem.pk)
    return form_page(
        request,
        form,
        "Report a property problem",
        photos=True,
        subtitle="Administrators will receive an email. Members can comment and help resolve it.",
    )


@member_required
def problem_detail(request, pk):
    problem = get_object_or_404(
        Problem.objects.select_related("reporter", "working_on", "resource"), pk=pk
    )
    form = forms.ProblemUpdateForm(request.POST or None, initial={"status": problem.status})
    if request.method == "POST" and form.is_valid():
        try:
            services.update_problem(
                request.user,
                pk,
                form.cleaned_data["status"],
                form.cleaned_data["comment"],
                form.cleaned_data["claim"],
                services.prepare_photos(request.FILES.getlist("photos")),
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("problem_detail", pk=pk)
    subscription = ProblemSubscription.objects.filter(problem=problem, user=request.user).first()
    return render(
        request,
        "lodge/problem.html",
        {"problem": problem, "form": form, "subscribed": subscription and subscription.enabled},
    )


@member_required
@require_POST
def subscription(request, pk):
    problem = get_object_or_404(Problem, pk=pk)
    ProblemSubscription.objects.update_or_create(
        problem=problem, user=request.user, defaults={"enabled": request.POST.get("enabled") == "1"}
    )
    return redirect("problem_detail", pk=pk)


@member_required
def photo(request, pk):
    image = get_object_or_404(ProblemPhoto, pk=pk)
    response = FileResponse(image.image.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    response["Content-Disposition"] = 'inline; filename="problem-photo.jpg"'
    return response


@member_required
def supplies(request):
    return render(
        request,
        "lodge/supplies.html",
        {
            "items": Consumable.objects.filter(active=True).select_related("updated_by"),
            "commitments": Commitment.objects.select_related("item", "member", "booking"),
            "today": timezone.localdate(),
        },
    )


@member_required
def stock(request, pk):
    item = get_object_or_404(Consumable, pk=pk, active=True)
    form = forms.StockForm(
        request.POST or None, initial={"level": item.level, "revision": item.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.observe_stock(request.user, pk, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("supplies")
    return form_page(
        request,
        form,
        f"Stock: {item.name}",
        observations=item.observations.select_related("author")[:30],
        subtitle="Record what you observed on site. A planned delivery does not change the stock level.",
    )


@member_required
def commitment_edit(request, pk=None):
    commitment = get_object_or_404(Commitment, pk=pk, member=request.user) if pk else None
    form = forms.CommitmentForm(request.POST or None, user=request.user, instance=commitment)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_commitment(
                request.user, form.cleaned_data, pk, form.cleaned_data.get("revision")
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if form.cleaned_data["status"] == "delivered":
                messages.success(
                    request, "Thank you! Please record the stock level after restocking."
                )
                return redirect("stock", pk=form.cleaned_data["item"].pk)
            return redirect("supplies")
    return form_page(request, form, "Update restocking plan" if pk else "I’ll bring supplies")


@admin_required
def members(request):
    if request.method == "POST":
        try:
            if request.POST.get("decision") in ("promote", "demote"):
                services.change_admin_role(
                    request.user,
                    int(request.POST.get("user", 0)),
                    request.POST["decision"] == "promote",
                )
            else:
                services.decide_member(
                    request.user,
                    int(request.POST.get("user", 0)),
                    request.POST.get("decision"),
                    request.POST.get("reason", ""),
                )
        except (ValidationError, ValueError, User.DoesNotExist) as error:
            messages.error(
                request,
                " ".join(error.messages)
                if isinstance(error, ValidationError)
                else "Member not found.",
            )
        else:
            messages.success(request, "Membership updated and email queued.")
        return redirect("members")
    show_deleted = request.GET.get("deleted") == "1"
    users = User.objects.order_by("status", "name")
    users = users.filter(deleted_at__isnull=not show_deleted)
    return render(request, "lodge/members.html", {"members": users, "show_deleted": show_deleted})


@admin_required
def member_edit(request, pk=None):
    member = get_object_or_404(User, pk=pk, deleted_at__isnull=True) if pk else None
    if (
        member
        and member.is_superuser
        and member.pk != request.user.pk
        and not request.user.is_superuser
    ):
        raise PermissionDenied
    form = user_admin.MemberForm(request.POST or None, member=member, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            saved = user_admin.save_member(request.user, form.cleaned_data, pk)
        except ValidationError as error:
            form.add_error(None, " ".join(error.messages))
        except IntegrityError:
            form.add_error("email", "This email address is already in use.")
        else:
            if saved.pk == request.user.pk and form.cleaned_data.get("password1"):
                update_session_auth_hash(request, saved)
            messages.success(request, "User updated." if pk else "User added.")
            if form.cleaned_data.get("send_setup") or (
                not pk and not form.cleaned_data.get("password1")
            ):
                messages.info(
                    request,
                    "Password setup email queued. Delivery uses the configured email backend.",
                )
            return redirect("members")
    return render(request, "lodge/member_form.html", {"form": form, "member": member})


@admin_required
def member_delete(request, pk):
    member = get_object_or_404(User, pk=pk, deleted_at__isnull=True)
    form = user_admin.DeleteMemberForm(
        request.POST or None, initial={"revision": member.admin_revision}
    )
    upcoming = user_admin.upcoming_reservations(member)
    protected = member.pk == request.user.pk or member.is_superuser
    if request.method == "POST" and form.is_valid():
        try:
            user_admin.delete_member(request.user, pk, form.cleaned_data["revision"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(
                request, f"{member.name}'s account was deleted. Shared history has been retained."
            )
            return redirect("members")
    return render(
        request,
        "lodge/member_delete.html",
        {
            "member": member,
            "form": form,
            "upcoming": upcoming,
            "protected": protected,
            "plans": Commitment.objects.filter(member=member, status="planned"),
        },
    )


@admin_required
def resources(request):
    return render(
        request,
        "lodge/resources.html",
        {
            "rooms": Resource.objects.all(),
            "blocks": MaintenanceBlock.objects.select_related("resource", "creator").order_by(
                "arrival"
            ),
        },
    )


@admin_required
def resource_edit(request, pk=None):
    instance = get_object_or_404(Resource, pk=pk) if pk else None
    form = forms.ResourceForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        try:
            services.configure_resource(request.user, pk, form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("resources")
    return form_page(request, form, "Configure bedroom")


@admin_required
def block_new(request):
    form = forms.BlockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.block_resource(request.user, form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("resources")
    return form_page(request, form, "Block a bedroom for maintenance")


@admin_required
@require_POST
def block_delete(request, pk):
    get_object_or_404(MaintenanceBlock, pk=pk)
    services.remove_block(request.user, pk)
    return redirect("resources")


@admin_required
def consumable_edit(request, pk=None):
    instance = get_object_or_404(Consumable, pk=pk) if pk else None
    form = forms.ConsumableForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if instance:
                # Only update metadata, preserving concurrent stock observations.
                Consumable.objects.select_for_update().get(pk=pk)
                Consumable.objects.filter(pk=pk).update(**form.cleaned_data)
                item = Consumable.objects.get(pk=pk)
            else:
                item = form.save()
            services.audit(request.user, "supply_configured", item)
        return redirect("admin_supplies")
    return form_page(request, form, "Configure supply item")


@admin_required
def admin_supplies(request):
    return render(request, "lodge/admin_supplies.html", {"items": Consumable.objects.all()})


@admin_required
def admin_bookings(request):
    bookings = Booking.objects.select_related("owner").prefetch_related("reservations__resource")
    return render(request, "lodge/admin_bookings.html", {"bookings": bookings[:200]})


@admin_required
def admin_booking_detail(request, pk):
    return booking_detail(request, pk, admin_mode=True)


@sensitive_post_parameters("password")
@admin_required
def email_settings(request):
    config = email_config.current_configuration()
    form = email_config.EmailSettingsForm(request.POST or None, config=config)
    if request.method == "POST" and form.is_valid():
        try:
            email_config.save_configuration(
                request.user, form.cleaned_data, send_test=request.POST.get("action") == "test"
            )
        except ValidationError as error:
            form.add_error(None, " ".join(error.messages))
        else:
            messages.success(
                request, "Email settings saved. Changes apply to the next queued message."
            )
            if request.POST.get("action") == "test":
                messages.info(
                    request,
                    "A test email is queued to your account. Check its delivery status below.",
                )
            return redirect("email_settings")
    response = render(
        request,
        "lodge/email_settings.html",
        {
            "form": form,
            "config": config,
            "tests": Notification.objects.filter(
                subject__endswith=email_config.TEST_SUBJECT, recipient=request.user.email
            ).order_by("-created_at")[:5],
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@sensitive_post_parameters("old_password", "new_password1", "new_password2", "current_password")
@member_required
def my_profile(request):
    action = request.POST.get("action", "profile") if request.method == "POST" else None
    form = profiles.ProfileForm(request.POST if action == "profile" else None, member=request.user)
    password_form = profiles.ProfilePasswordChangeForm(
        request.user, data=request.POST if action == "password" else None
    )
    email_form = profiles.EmailChangeForm(
        request.POST if action == "email" else None, member=request.user
    )
    selected = {"profile": form, "password": password_form, "email": email_form}.get(action)
    if selected is not None and selected.is_valid():
        try:
            if action == "profile":
                profiles.save_profile(request.user, selected.cleaned_data)
                notice = "Your profile and sharing preferences were saved."
            elif action == "password":
                member = profiles.change_password(request.user, selected.cleaned_data)
                update_session_auth_hash(request, member)
                notice = "Your password was changed. Other sessions will need to sign in again."
            else:
                profiles.request_email_change(request.user, selected.cleaned_data)
                notice = "Check your new email address for a verification link. Your current sign-in address is unchanged until you confirm."
        except ValidationError as error:
            selected.add_error(None, " ".join(error.messages))
        else:
            messages.success(request, notice)
            return redirect("my_profile")
    elif action == "cancel_email":
        try:
            profiles.cancel_email_change(request.user, int(request.POST.get("revision", 0)))
        except (ValidationError, ValueError) as error:
            email_form.add_error(
                None,
                " ".join(error.messages)
                if isinstance(error, ValidationError)
                else "Invalid revision. Reload this page.",
            )
        else:
            messages.success(request, "Pending email change canceled.")
            return redirect("my_profile")
    response = render(
        request,
        "lodge/my_profile.html",
        {"form": form, "password_form": password_form, "email_form": email_form},
    )
    response["Cache-Control"] = "private, no-store"
    return response


@member_required
def confirm_email_change(request, token):
    try:
        new_email = profiles.validate_email_change(request.user, token)
        if request.method == "POST":
            profiles.confirm_email_change(request.user, token)
            messages.success(
                request,
                "Email address verified and changed. Use the new address for future sign-ins.",
            )
            return redirect("my_profile")
    except (ValidationError, IntegrityError) as error:
        text = (
            " ".join(error.messages)
            if isinstance(error, ValidationError)
            else "This email address is no longer available."
        )
        response = render(
            request,
            "lodge/message.html",
            {"title": "Unable to change email", "text": text},
            status=400,
        )
    else:
        response = render(request, "lodge/confirm_email_change.html", {"new_email": new_email})
    response["Cache-Control"] = "private, no-store"
    # Keep the signed token out of Referer headers without suppressing the
    # origin needed by CSRF checks on confirmation and navigation forms.
    response["Referrer-Policy"] = "strict-origin"
    return response


@member_required
def member_directory(request):
    response = render(
        request, "lodge/member_directory.html", {"entries": profiles.directory_entries()}
    )
    response["Cache-Control"] = "private, no-store"
    return response
