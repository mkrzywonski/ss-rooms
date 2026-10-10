from datetime import timedelta

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordResetForm
from django.utils import timezone

from .models import (
    Booking,
    Commitment,
    Consumable,
    Problem,
    Resource,
    User,
)
from .services import queue_email


class RegisterForm(forms.Form):
    name = forms.CharField(max_length=120, label="Your name")
    email = forms.EmailField()
    password1 = forms.CharField(
        widget=forms.PasswordInput, label="Password", help_text="Use at least 12 characters."
    )
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm password")
    connection = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
        label="Your connection to Stellar Skies",
    )
    invitation = forms.UUIDField(required=False, widget=forms.HiddenInput)
    website = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        data = super().clean()
        if data.get("website"):
            raise forms.ValidationError("Unable to process this registration.")
        if data.get("password1") != data.get("password2"):
            self.add_error("password2", "Passwords must match.")
        if data.get("password1"):
            password_validation.validate_password(
                data["password1"], User(email=data.get("email", ""), name=data.get("name", ""))
            )
        return data


class QueuedPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        return (
            user
            for user in super().get_users(email)
            if user.status not in ("suspended", "rejected")
        )

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        from urllib.parse import urlsplit

        from django.conf import settings
        from django.template.loader import render_to_string

        url = urlsplit(settings.PUBLIC_URL)
        context.update(domain=url.netloc, protocol=url.scheme)
        subject = "".join(render_to_string(subject_template_name, context).splitlines())
        queue_email([to_email], subject, render_to_string(email_template_name, context))


class DateForm(forms.Form):
    arrival = forms.DateField(label="First night", widget=forms.DateInput(attrs={"type": "date"}))
    departure = forms.DateField(
        label="Departure date", widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean(self):
        data = super().clean()
        if data.get("arrival") and data.get("departure") and data["departure"] <= data["arrival"]:
            raise forms.ValidationError("Departure must be after the first night.")
        return data


class BookingForm(DateForm):
    party_size = forms.IntegerField(
        min_value=1, max_value=100, initial=1, label="Total people in your party"
    )
    guest_names = forms.CharField(max_length=300, required=False, label="Guest names")
    notes = forms.CharField(
        required=False,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Arrival plans or other details to share with members.",
    )
    revision = forms.IntegerField(required=False, widget=forms.HiddenInput)
    reason = forms.CharField(
        required=False,
        max_length=300,
        label="Reason for change",
        help_text="Required when an administrator changes another member's booking.",
    )

    def __init__(self, *args, booking=None, room=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rooms = [room] if room else []
        if not booking:
            today = timezone.localdate()
            self.fields["arrival"].widget.attrs["min"] = today.isoformat()
            self.fields["departure"].widget.attrs["min"] = (today + timedelta(days=1)).isoformat()
        initial_rows = {}
        if booking:
            active = list(booking.reservations.filter(canceled_at__isnull=True))
            initial_rows = {r.resource_id: r for r in active}
            self.rooms = list(Resource.objects.filter(pk__in=initial_rows))
            self.initial.update(
                party_size=booking.party_size,
                guest_names=booking.guest_names,
                notes=booking.notes,
                revision=booking.revision,
            )
            if active:
                self.initial.update(
                    arrival=min(r.arrival for r in active),
                    departure=max(r.departure for r in active),
                )
        for room in self.rooms:
            row = initial_rows.get(room.pk)
            self.fields[f"places_{room.pk}"] = forms.IntegerField(
                min_value=1,
                max_value=room.capacity,
                initial=row.places if row else 1,
                label=f"{room.name}: sleeping places",
                help_text=f"Capacity: {room.capacity}. {room.description}",
            )
            if room.sharing_enabled:
                self.fields[f"share_{room.pk}"] = forms.BooleanField(
                    required=False,
                    initial=row.share if row else False,
                    label=f"Allow others to share {room.name}",
                    help_text="Private unless you opt in.",
                )

    def rows(self):
        return [
            {
                "resource": room.pk,
                "arrival": self.cleaned_data["arrival"],
                "departure": self.cleaned_data["departure"],
                "places": self.cleaned_data[f"places_{room.pk}"],
                "share": self.cleaned_data.get(f"share_{room.pk}", False),
            }
            for room in self.rooms
            if self.cleaned_data.get(f"places_{room.pk}")
        ]


class ReservationForm(DateForm):
    places = forms.IntegerField(min_value=1)
    share = forms.BooleanField(required=False, label="Allow sharing")
    revision = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(required=False, max_length=300, label="Reason for change")


class ResourceForm(forms.ModelForm):
    class Meta:
        model = Resource
        fields = ["name", "description", "capacity", "sharing_enabled", "active"]


class BlockForm(DateForm):
    resource = forms.ModelChoiceField(
        queryset=Resource.objects.filter(active=True), label="Bedroom"
    )
    reason = forms.CharField(max_length=300)


class ProblemForm(forms.ModelForm):
    class Meta:
        model = Problem
        fields = ["title", "location", "resource", "urgency", "description"]
        labels = {"resource": "Related bedroom (optional)"}
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}


class ProblemUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=Problem.Status.choices)
    comment = forms.CharField(
        required=False, max_length=10000, widget=forms.Textarea(attrs={"rows": 3})
    )
    claim = forms.BooleanField(required=False, label="I am working on this")


class StockForm(forms.Form):
    level = forms.ChoiceField(choices=Consumable.Level.choices)
    note = forms.CharField(required=False, max_length=300)
    revision = forms.IntegerField(widget=forms.HiddenInput)


class ConsumableForm(forms.ModelForm):
    class Meta:
        model = Consumable
        fields = ["name", "description", "location", "active"]


class CommitmentForm(forms.ModelForm):
    revision = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Commitment
        fields = ["item", "expected_date", "description", "booking", "status"]
        widgets = {"expected_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"description": "What will you bring?", "booking": "Related booking (optional)"}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = Consumable.objects.filter(active=True)
        self.fields["booking"].queryset = Booking.objects.filter(
            owner=user,
            reservations__canceled_at__isnull=True,
            reservations__departure__gt=timezone.localdate(),
        ).distinct()
        self.fields["booking"].label_from_instance = lambda b: f"Booking #{b.pk}"
        if self.instance.pk:
            self.initial["revision"] = self.instance.revision
