from django.core.management.base import BaseCommand

from lodge.models import Consumable, Resource


class Command(BaseCommand):
    help = "Create placeholder bedrooms and initial supply items without changing existing records."

    def handle(self, *args, **options):
        for name in ("Bedroom 1", "Bedroom 2", "Bedroom 3"):
            Resource.objects.get_or_create(
                name=name,
                defaults={
                    "description": "Confirm this room's sleeping arrangements before enabling sharing."
                },
            )
        for name in ("Toilet paper", "Paper towels", "Laundry detergent"):
            Consumable.objects.get_or_create(name=name)
        self.stdout.write(self.style.SUCCESS("Bedrooms and supplies are ready."))
