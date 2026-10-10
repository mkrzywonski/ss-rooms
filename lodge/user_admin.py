"""Administrator-managed accounts and deletion that preserves shared history."""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import Commitment, Problem, Reservation, User
from .services import absolute, audit, lock_user_admin, queue_email


class MemberForm(forms.Form):
    name = forms.CharField(max_length=120, label="Name")
    email = forms.EmailField()
    address = forms.CharField(
        required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )
    share_address = forms.BooleanField(
        required=False,
        label="Share my address with other members",
        help_text="Private by default. Administrators can still access it.",
    )
    phone_number = forms.CharField(required=False, max_length=40)
    share_phone_number = forms.BooleanField(
        required=False,
        label="Share my phone number with other members",
        help_text="Private by default. Administrators can still access it.",
    )
    observatory_number = forms.CharField(required=False, max_length=40)
    connection = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Connection to Stellar Skies",
    )
    status = forms.ChoiceField(
        choices=User.Status.choices,
        initial="approved",
        help_text="Accounts added by an administrator can be approved directly.",
    )
    is_active = forms.BooleanField(required=False, initial=True, label="Account enabled")
    is_staff = forms.BooleanField(required=False, label="Administrator")
    password1 = forms.CharField(
        required=False,
        widget=forms.PasswordInput,
        label="New password",
        help_text="Optional. At least 12 characters. Leave blank to preserve an existing password; new users receive a setup link.",
    )
    password2 = forms.CharField(
        required=False, widget=forms.PasswordInput, label="Confirm new password"
    )
    send_setup = forms.BooleanField(required=False, label="Send password setup/reset email")
    revision = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, member=None, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.member = member
        if member:
            self.initial.update(
                {
                    key: getattr(member, key)
                    for key in (
                        "name",
                        "email",
                        "address",
                        "phone_number",
                        "share_address",
                        "share_phone_number",
                        "observatory_number",
                        "connection",
                        "status",
                        "is_active",
                        "is_staff",
                    )
                }
            )
            self.initial["revision"] = member.admin_revision
            if member.pk == actor.pk or member.is_superuser:
                for key in ("status", "is_active", "is_staff"):
                    self.fields[key].disabled = True
        else:
            self.initial["send_setup"] = True

        if not member or member.pk != actor.pk:
            for key in ("share_address", "share_phone_number"):
                self.fields[key].disabled = True
                self.fields[
                    key
                ].help_text = "Only the account owner can change this preference in My profile."

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        existing = User.objects.filter(email__iexact=email)
        if self.member:
            existing = existing.exclude(pk=self.member.pk)
        if existing.exists():
            raise forms.ValidationError(
                "This email address is already in use, including retained deleted accounts."
            )
        return email

    def clean(self):
        data = super().clean()
        if data.get("password1") != data.get("password2"):
            self.add_error("password2", "Passwords must match.")
        if data.get("password1"):
            password_validation.validate_password(
                data["password1"], User(email=data.get("email", ""), name=data.get("name", ""))
            )
        if data.get("is_staff") and (data.get("status") != "approved" or not data.get("is_active")):
            raise forms.ValidationError("Administrators must be enabled and approved.")
        if data.get("send_setup") and (
            not data.get("is_active") or data.get("status") != "approved"
        ):
            raise forms.ValidationError(
                "Password setup emails are available for enabled, approved accounts."
            )
        return data


class DeleteMemberForm(forms.Form):
    confirm = forms.BooleanField(label="Delete this account and revoke its access")
    revision = forms.IntegerField(widget=forms.HiddenInput)


def upcoming_reservations(member):
    return Reservation.objects.filter(
        booking__owner=member, canceled_at__isnull=True, departure__gt=timezone.localdate()
    ).select_related("booking", "resource")


def queue_password_setup(member):
    token = default_token_generator.make_token(member)
    uid = urlsafe_base64_encode(force_bytes(member.pk))
    link = absolute(reverse("password_reset_confirm", kwargs={"uidb64": uid, "token": token}))
    queue_email(
        [member.email],
        "Set your Lodge password",
        f"An administrator created or updated your Lodge account.\n\nChoose your password using this link:\n{link}\n\nIf you did not expect this, contact a Stellar Skies administrator.",
    )


@transaction.atomic
def save_member(actor, data, member_id=None):
    actor = lock_user_admin(actor)
    member = User.objects.select_for_update(no_key=True).get(pk=member_id) if member_id else User()
    created = member_id is None
    if member.deleted_at:
        raise ValidationError("Deleted accounts cannot be edited.")
    if member.is_superuser and actor.pk != member.pk and not actor.is_superuser:
        raise PermissionDenied
    if not created and member.admin_revision != data.get("revision"):
        raise ValidationError(
            "This account changed while your form was open. Reload before saving."
        )
    if not created and (member.pk == actor.pk or member.is_superuser):
        if any(data[key] != getattr(member, key) for key in ("status", "is_active", "is_staff")):
            raise ValidationError("Your own access and superuser access cannot be changed here.")
    if data["status"] not in User.Status.values:
        raise ValidationError("Invalid membership status.")
    if data["is_staff"] and (data["status"] != "approved" or not data["is_active"]):
        raise ValidationError("Administrators must be enabled and approved.")
    email = data["email"].strip().lower()
    if User.objects.filter(email__iexact=email).exclude(pk=member.pk).exists():
        raise ValidationError(
            "This email address is already in use, including retained deleted accounts."
        )
    for key in ("share_address", "share_phone_number"):
        value = data.get(key, getattr(member, key))
        if member.pk != actor.pk and value != getattr(member, key):
            raise ValidationError("Only the account owner can change contact sharing preferences.")
    old_email = member.email
    before = (
        {
            key: getattr(member, key)
            for key in (
                "name",
                "email",
                "address",
                "phone_number",
                "share_address",
                "share_phone_number",
                "observatory_number",
                "connection",
                "status",
                "is_active",
                "is_staff",
            )
        }
        if not created
        else {}
    )
    for key in ("name", "connection", "status", "is_active", "is_staff"):
        setattr(member, key, data[key])
    for key in ("address", "phone_number", "observatory_number"):
        setattr(member, key, data.get(key, getattr(member, key)))
    for key in ("share_address", "share_phone_number"):
        setattr(member, key, data.get(key, getattr(member, key)))
    member.email = email
    password = data.get("password1", "")
    if password != data.get("password2", ""):
        raise ValidationError("Passwords must match.")
    if password:
        password_validation.validate_password(password, member)
        member.set_password(password)
    elif created:
        member.set_unusable_password()
    if not created:
        member.admin_revision += 1
    if password or old_email != member.email:
        member.pending_email = ""
        member.email_change_token = ""
    member.full_clean(exclude=["password"])
    member.save()
    after = (
        {key: getattr(member, key) for key in before}
        if before
        else {
            "name": member.name,
            "email": member.email,
            "address": member.address,
            "phone_number": member.phone_number,
            "share_address": member.share_address,
            "share_phone_number": member.share_phone_number,
            "observatory_number": member.observatory_number,
            "status": member.status,
            "is_active": member.is_active,
            "is_staff": member.is_staff,
        }
    )
    audit(
        actor,
        "member_created" if created else "member_edited",
        member,
        before=before,
        after=after,
        password_changed=bool(password),
    )
    if old_email and old_email != member.email:
        queue_email(
            [old_email],
            "Your account email changed",
            f"{actor.name} changed your Lodge sign-in email to {member.email}. Contact an administrator if this was unexpected.",
        )
    if (
        member.status == "approved"
        and member.is_active
        and (data.get("send_setup") or (created and not password))
    ):
        queue_password_setup(member)
    elif (
        created
        or before.get("status") != member.status
        or before.get("is_active") != member.is_active
    ):
        queue_email(
            [member.email],
            "Your Lodge account",
            f"Your account has been {'created' if created else 'updated'} by {actor.name}.\nStatus: {member.get_status_display()}\nAccount enabled: {'yes' if member.is_active else 'no'}\n\n{absolute(reverse('login'))}",
        )
    return member


@transaction.atomic
def delete_member(actor, member_id, revision):
    actor = lock_user_admin(actor)
    member = User.objects.select_for_update(no_key=True).get(pk=member_id)
    if member.pk == actor.pk:
        raise ValidationError("You cannot delete your own account.")
    if member.is_superuser:
        raise ValidationError("Superuser accounts cannot be deleted through this page.")
    if member.deleted_at:
        raise ValidationError("This account is already deleted.")
    if member.admin_revision != revision:
        raise ValidationError("This account changed. Reload the confirmation page.")
    if upcoming_reservations(member).exists():
        raise ValidationError(
            "Resolve this member's upcoming reservations before deleting the account."
        )
    member.deleted_at = timezone.now()
    member.is_active = False
    member.is_staff = False
    member.admin_revision += 1
    member.set_unusable_password()
    member.save(update_fields=["deleted_at", "is_active", "is_staff", "admin_revision", "password"])
    canceled = Commitment.objects.filter(member=member, status="planned").update(
        status="canceled", needs_review=False, revision=F("revision") + 1
    )
    Problem.objects.filter(working_on=member).update(working_on=None)
    audit(
        actor,
        "member_deleted",
        member,
        name=member.name,
        email=member.email,
        restocking_plans_canceled=canceled,
    )
    queue_email(
        [member.email],
        "Lodge account deleted",
        f"{actor.name} deleted your Lodge account. You can no longer sign in. Existing reservation and property history is retained.",
    )
    return member
