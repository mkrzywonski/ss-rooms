from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from lodge.models import Invitation
from lodge.services import absolute, queue_email


class Command(BaseCommand):
    help = "Email an invitation; membership approval is still required."

    def add_arguments(self, parser):
        parser.add_argument("email")

    @transaction.atomic
    def handle(self, *args, **options):
        invitation = Invitation.objects.create(
            email=options["email"].strip().lower(), expires_at=timezone.now() + timedelta(days=14)
        )
        invitation.full_clean()
        queue_email(
            [invitation.email],
            "Invitation to the Lodge",
            f"Register here within 14 days:\n\n{absolute('/register/')}?invitation={invitation.token}\n\nUse this email address. Administrator approval is required.",
        )
        self.stdout.write(self.style.SUCCESS("Invitation queued."))
