import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required.")
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, status="approved")
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    class Status(models.TextChoices):
        UNVERIFIED = "unverified", "Email unverified"
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"

    username = None
    email = models.EmailField(unique=True)
    pending_email = models.EmailField(blank=True)
    email_change_token = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=120)
    address = models.TextField(blank=True, max_length=2000, validators=[MaxLengthValidator(2000)])
    phone_number = models.CharField(max_length=40, blank=True)
    share_address = models.BooleanField(default=False)
    share_phone_number = models.BooleanField(default=False)
    observatory_number = models.CharField(max_length=40, blank=True)
    connection = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UNVERIFIED)
    deleted_at = models.DateTimeField(null=True, blank=True)
    admin_revision = models.PositiveIntegerField(default=1)
    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="user_email_case_unique")]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.email


class Invitation(models.Model):
    email = models.EmailField()
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.email


class Resource(models.Model):
    kind = models.CharField(max_length=30, default="bedroom", editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    capacity = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    sharing_enabled = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(capacity__gte=1), name="resource_capacity_positive"
            )
        ]

    def __str__(self):
        return self.name


class Booking(models.Model):
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="bookings")
    party_size = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    guest_names = models.CharField(max_length=300, blank=True)
    notes = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def active(self):
        return self.reservations.filter(canceled_at__isnull=True).exists()

    def __str__(self):
        return f"Booking #{self.pk} — {self.owner}"


class Reservation(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="reservations")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="reservations")
    arrival = models.DateField()
    departure = models.DateField()
    places = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    share = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["arrival", "resource__name"]
        indexes = [models.Index(fields=["resource", "arrival", "departure"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(departure__gt=models.F("arrival")),
                name="reservation_dates_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(places__gte=1), name="reservation_places_positive"
            ),
        ]

    def __str__(self):
        return f"{self.resource}: {self.arrival} to {self.departure}"


class MaintenanceBlock(models.Model):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    arrival = models.DateField()
    departure = models.DateField()
    reason = models.CharField(max_length=300)
    creator = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(departure__gt=models.F("arrival")), name="block_dates_valid"
            )
        ]

    def __str__(self):
        return f"{self.resource}: {self.arrival} — {self.reason}"


class Problem(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PROGRESS = "progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    reporter = models.ForeignKey(User, on_delete=models.PROTECT, related_name="problems")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, null=True, blank=True)
    location = models.CharField(max_length=160)
    title = models.CharField(max_length=160)
    description = models.TextField()
    urgency = models.CharField(
        max_length=12,
        choices=[("routine", "Routine"), ("soon", "Needs attention soon"), ("urgent", "Urgent")],
        default="routine",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    working_on = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="working_problems"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class ProblemUpdate(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name="updates")
    author = models.ForeignKey(User, on_delete=models.PROTECT)
    comment = models.TextField(blank=True)
    from_status = models.CharField(max_length=12)
    to_status = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class ProblemPhoto(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name="photos")
    author = models.ForeignKey(User, on_delete=models.PROTECT)
    image = models.ImageField(upload_to="problems/")
    created_at = models.DateTimeField(auto_now_add=True)


class ProblemSubscription(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name="subscriptions")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["problem", "user"], name="problem_subscriber_unique")
        ]


class Consumable(models.Model):
    class Level(models.TextChoices):
        PLENTY = "plenty", "Plenty"
        LOW = "low", "Low"
        OUT = "out", "Out"
        UNKNOWN = "unknown", "Unknown"

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.UNKNOWN)
    updated_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class StockObservation(models.Model):
    item = models.ForeignKey(Consumable, on_delete=models.PROTECT, related_name="observations")
    author = models.ForeignKey(User, on_delete=models.PROTECT)
    level = models.CharField(max_length=10, choices=Consumable.Level.choices)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Commitment(models.Model):
    item = models.ForeignKey(Consumable, on_delete=models.PROTECT, related_name="commitments")
    member = models.ForeignKey(User, on_delete=models.PROTECT)
    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True)
    expected_date = models.DateField()
    description = models.CharField(max_length=300, blank=True)
    status = models.CharField(
        max_length=12,
        choices=[("planned", "Planned"), ("delivered", "Delivered"), ("canceled", "Canceled")],
        default="planned",
    )
    needs_review = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["expected_date"]

    @property
    def overdue(self):
        return self.status == "planned" and self.expected_date < timezone.localdate()


class Notification(models.Model):
    recipient = models.EmailField()
    subject = models.CharField(max_length=200)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed = models.BooleanField(default=False)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["created_at"]


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField(max_length=100)
    target = models.CharField(max_length=120)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class RateBucket(models.Model):
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()


class EmailConfiguration(models.Model):
    """Singleton transport settings; credentials are never exposed as model fields in admin."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    host = models.CharField(max_length=253, blank=True)
    port = models.PositiveIntegerField(default=587)
    security = models.CharField(
        max_length=8, choices=[("starttls", "STARTTLS"), ("ssl", "TLS / SSL")], default="starttls"
    )
    username = models.CharField(max_length=254, blank=True)
    encrypted_password = models.TextField(blank=True, editable=False)
    from_address = models.EmailField(blank=True)
    from_name = models.CharField(max_length=120, blank=True)
    revision = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="email_configuration_singleton")
        ]

    def __str__(self):
        return "Email configuration"
