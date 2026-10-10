import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from lodge.models import RateBucket, User
from lodge.notifications import deliver_one


class Command(BaseCommand):
    help = "Deliver queued email, retry failures, and clean up expired registration data."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        last_cleanup = 0
        while True:
            if time.monotonic() - last_cleanup > 3600:
                RateBucket.objects.filter(expires_at__lt=timezone.now()).delete()
                # Keep audit history; deactivate stale applicants rather than deleting referenced users.
                User.objects.filter(
                    status="unverified", date_joined__lt=timezone.now() - timedelta(days=30)
                ).update(is_active=False)
                last_cleanup = time.monotonic()
            if not deliver_one():
                if options["once"]:
                    return
                time.sleep(5)
