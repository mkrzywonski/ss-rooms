import hashlib
import time
from datetime import timedelta

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from .models import RateBucket


class AuthRateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        paths = {
            "/admin/login/": 30,
            "/accounts/login/": 30,
            "/register/": 8,
            "/accounts/password_reset/": 8,
            "/account/resend/": 5,
        }
        if request.method == "POST" and request.path in paths:
            # Only trust REMOTE_ADDR; the proxy must overwrite any forwarded IP header.
            window = int(time.time()) // 3600
            source = request.META.get("REMOTE_ADDR", "")
            key = hashlib.sha256(f"{request.path}:{source}:{window}".encode()).hexdigest()
            with transaction.atomic():
                bucket, _ = RateBucket.objects.get_or_create(
                    key=key, defaults={"expires_at": timezone.now() + timedelta(hours=2)}
                )
                bucket = RateBucket.objects.select_for_update().get(pk=bucket.pk)
                bucket.count += 1
                bucket.save(update_fields=["count"])
            if bucket.count > paths[request.path]:
                return HttpResponse(
                    "Too many attempts. Please try again later.",
                    status=429,
                    headers={"Retry-After": "3600"},
                )
        return self.get_response(request)
