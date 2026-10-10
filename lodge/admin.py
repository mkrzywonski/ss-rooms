from django.contrib import admin
from django.utils import timezone

from .models import (
    AuditEvent,
    Booking,
    Commitment,
    Consumable,
    Invitation,
    MaintenanceBlock,
    Notification,
    Problem,
    Reservation,
    Resource,
    StockObservation,
    User,
)

admin.site.site_header = "Stellar Skies Lodge administration"
admin.site.site_title = "Lodge administration"
admin.site.has_permission = lambda request: (
    request.user.is_active and request.user.is_staff and request.user.status == "approved"
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_view_permission(self, request, obj=None):
        return request.user.is_staff and request.user.status == "approved"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(User)
class MemberAdmin(ReadOnlyAdmin):
    list_display = ["name", "email", "status", "is_staff", "is_active"]
    list_filter = ["status", "is_staff"]
    search_fields = ["name", "email"]
    exclude = ["password"]


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = [
        "recipient",
        "subject",
        "created_at",
        "attempts",
        "sent_at",
        "failed",
        "last_error",
    ]
    list_filter = ["failed", "sent_at"]
    actions = ["retry"]

    def has_change_permission(self, request, obj=None):
        return obj is None and request.user.is_staff and request.user.status == "approved"

    @admin.action(description="Retry selected failed, unsent messages")
    def retry(self, request, queryset):
        count = queryset.filter(sent_at__isnull=True, failed=True).update(
            failed=False, attempts=0, next_attempt=timezone.now(), locked_until=None
        )
        self.message_user(request, f"{count} messages queued for retry.")


@admin.register(AuditEvent)
class AuditAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "actor", "action", "target"]
    list_filter = ["action"]


for model in (
    Booking,
    Reservation,
    Resource,
    MaintenanceBlock,
    Problem,
    Consumable,
    StockObservation,
    Commitment,
    Invitation,
):
    admin.site.register(model, ReadOnlyAdmin)
