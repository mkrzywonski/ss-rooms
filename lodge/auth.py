from django.contrib.auth.backends import ModelBackend


class MemberBackend(ModelBackend):
    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and user.status not in ("suspended", "rejected")
