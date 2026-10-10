import logging
from datetime import timedelta

from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .email_config import transport
from .models import Notification

logger = logging.getLogger(__name__)


def deliver_one():
    now = timezone.now()
    with transaction.atomic():
        message = (
            Notification.objects.select_for_update(skip_locked=True)
            .filter(sent_at__isnull=True, failed=False, next_attempt__lte=now)
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now))
            .first()
        )
        if not message:
            return False
        message.attempts += 1
        message.locked_until = now + timedelta(minutes=5)
        message.save(update_fields=["attempts", "locked_until"])
    try:
        connection, sender = transport()
        if (
            send_mail(
                message.subject,
                message.body,
                sender,
                [message.recipient],
                fail_silently=False,
                connection=connection,
            )
            != 1
        ):
            raise RuntimeError("Mail backend did not accept the message.")
    except Exception as error:
        # Store the type only: SMTP exceptions can include credentials or tokens.
        Notification.objects.filter(pk=message.pk).update(
            locked_until=None,
            failed=message.attempts >= 8,
            next_attempt=timezone.now() + timedelta(seconds=min(3600, 30 * 2**message.attempts)),
            last_error=type(error).__name__,
        )
        logger.warning("Notification %s failed: %s", message.pk, type(error).__name__)
    else:
        Notification.objects.filter(pk=message.pk).update(
            sent_at=timezone.now(), locked_until=None, last_error=""
        )
    return True
