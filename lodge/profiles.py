"""Owner-controlled contact information and privacy-filtered member directory."""

import uuid

from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.debug import sensitive_variables

from .models import User
from .services import absolute, audit, queue_email, require_member

CONTACT_FIELDS = (
    "address",
    "phone_number",
    "share_address",
    "share_phone_number",
    "observatory_number",
)


class ProfileForm(forms.Form):
    address = forms.CharField(
        required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )
    share_address = forms.BooleanField(required=False, label="Share my address with other members")
    phone_number = forms.CharField(required=False, max_length=40)
    share_phone_number = forms.BooleanField(
        required=False, label="Share my phone number with other members"
    )
    observatory_number = forms.CharField(
        required=False,
        max_length=40,
        help_text="Optional identifier, for example 07 or A-3. Visible in the member directory.",
    )
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.update({key: getattr(member, key) for key in CONTACT_FIELDS})
        self.initial["revision"] = member.admin_revision


@transaction.atomic
def save_profile(actor, data):
    # Lock and reload the authenticated owner, never a target ID supplied by a client.
    require_member(actor)
    member = User.objects.get(pk=actor.pk)
    form = ProfileForm(data, member=member)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    values = form.cleaned_data
    if values["revision"] != member.admin_revision:
        raise ValidationError(
            "Your profile changed while this form was open. Reload before saving."
        )
    changed = [key for key in CONTACT_FIELDS if getattr(member, key) != values[key]]
    for key in CONTACT_FIELDS:
        setattr(member, key, values[key])
    member.admin_revision += 1
    member.full_clean(exclude=["password"])
    member.save(update_fields=[*CONTACT_FIELDS, "admin_revision"])
    # Record the action, not historical copies of private contact details.
    audit(
        member,
        "profile_updated",
        member,
        fields_changed=changed,
        share_address=member.share_address,
        share_phone_number=member.share_phone_number,
    )
    return member


def directory_entries():
    # Redact in the query itself: private contact values never reach the template.
    return (
        User.objects.filter(status="approved", is_active=True, deleted_at__isnull=True)
        .annotate(
            visible_address=models.Case(
                models.When(share_address=True, then=models.F("address")),
                default=models.Value(""),
                output_field=models.TextField(),
            ),
            visible_phone_number=models.Case(
                models.When(share_phone_number=True, then=models.F("phone_number")),
                default=models.Value(""),
                output_field=models.CharField(),
            ),
        )
        .order_by("name", "pk")
        .values(
            "pk", "name", "email", "observatory_number", "visible_address", "visible_phone_number"
        )
    )


class ProfilePasswordChangeForm(PasswordChangeForm):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.initial["revision"] = user.admin_revision


@sensitive_variables("__ALL__")
@transaction.atomic
def change_password(actor, data):
    require_member(actor)
    member = User.objects.get(pk=actor.pk)
    form = ProfilePasswordChangeForm(member, data=data)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    if form.cleaned_data["revision"] != member.admin_revision:
        raise ValidationError(
            "Your account changed while this form was open. Reload before saving."
        )
    form.save(commit=False)
    member.admin_revision += 1
    member.pending_email = ""
    member.email_change_token = ""
    member.save(update_fields=["password", "admin_revision", "pending_email", "email_change_token"])
    audit(member, "password_changed", member)
    queue_email(
        [member.email],
        "Your Lodge password changed",
        "Your Lodge sign-in password was changed from My profile. If this was not you, contact an administrator immediately.",
    )
    return member


class EmailChangeForm(forms.Form):
    new_email = forms.EmailField(max_length=254, label="New email address")
    current_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        strip=False,
        label="Current password",
    )
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, member, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)
        self.initial["revision"] = member.admin_revision

    def clean_new_email(self):
        value = self.cleaned_data["new_email"].strip().lower()
        if value == self.member.email:
            raise forms.ValidationError("This is already your email address.")
        if User.objects.filter(email__iexact=value).exists():
            raise forms.ValidationError("This email address is already in use.")
        return value

    @sensitive_variables("__ALL__")
    def clean_current_password(self):
        value = self.cleaned_data["current_password"]
        if not self.member.check_password(value):
            raise forms.ValidationError("Your current password is incorrect.")
        return value


@sensitive_variables("__ALL__")
@transaction.atomic
def request_email_change(actor, data):
    require_member(actor)
    member = User.objects.get(pk=actor.pk)
    form = EmailChangeForm(data, member=member)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    if member.admin_revision != form.cleaned_data["revision"]:
        raise ValidationError("Your account changed. Reload before saving.")
    member.pending_email = form.cleaned_data["new_email"]
    member.email_change_token = uuid.uuid4().hex
    member.admin_revision += 1
    member.save(update_fields=["pending_email", "email_change_token", "admin_revision"])
    token = signing.dumps(
        {
            "id": member.pk,
            "email": member.email,
            "pending": member.pending_email,
            "nonce": member.email_change_token,
            "credential": salted_hmac("lodge.email-change", member.password).hexdigest(),
        },
        salt="profile-email-change",
        compress=True,
    )
    queue_email(
        [member.pending_email],
        "Confirm your new Lodge email address",
        "Confirm your new sign-in email using the link below. Sign in with your current email first. "
        "Your old email remains active until you confirm. This link expires in 48 hours.\n\n"
        + absolute(reverse("confirm_email_change", args=[token])),
    )
    queue_email(
        [member.email],
        "Lodge email change requested",
        "A change to your Lodge sign-in email was requested from My profile. "
        "Your current address remains active until the new one is verified. "
        "If this was not you, change your password and contact an administrator.",
    )
    audit(member, "email_change_requested", member)
    return member


def validate_email_change(member, token):
    try:
        data = signing.loads(token, salt="profile-email-change", max_age=48 * 3600)
        if data["id"] != member.pk:
            raise ValidationError(
                "This link belongs to a different account. Sign out, sign in with the account "
                "that requested the email change, then reopen the newest confirmation email.",
                code="wrong_account",
            )
        valid = (
            data["id"] == member.pk
            and data["email"] == member.email
            and data["pending"] == member.pending_email
            and bool(member.pending_email)
            and constant_time_compare(data["nonce"], member.email_change_token)
            and constant_time_compare(
                data["credential"], salted_hmac("lodge.email-change", member.password).hexdigest()
            )
        )
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValidationError(
            "This email-change link is invalid or expired. Request a new one from My profile."
        )
    return member.pending_email


@transaction.atomic
def confirm_email_change(actor, token):
    require_member(actor)
    member = User.objects.get(pk=actor.pk)
    new_email = validate_email_change(member, token)
    if User.objects.filter(email__iexact=new_email).exclude(pk=member.pk).exists():
        raise ValidationError(
            "This email address is no longer available. Request a different address."
        )
    old_email = member.email
    member.email = new_email
    member.pending_email = ""
    member.email_change_token = ""
    member.admin_revision += 1
    member.save(update_fields=["email", "pending_email", "email_change_token", "admin_revision"])
    queue_email(
        [old_email, new_email],
        "Your Lodge email address changed",
        "Your verified Lodge sign-in email address was changed from My profile. "
        "Use the new address to sign in. If this was not you, contact an administrator immediately.",
    )
    audit(member, "email_changed", member)
    return member


@transaction.atomic
def cancel_email_change(actor, revision):
    require_member(actor)
    member = User.objects.get(pk=actor.pk)
    if revision != member.admin_revision:
        raise ValidationError("Your account changed. Reload before saving.")
    member.pending_email = ""
    member.email_change_token = ""
    member.admin_revision += 1
    member.save(update_fields=["pending_email", "email_change_token", "admin_revision"])
    audit(member, "email_change_canceled", member)
