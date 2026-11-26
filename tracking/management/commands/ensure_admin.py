import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create/update an admin user from FLEET_ADMIN_USER / FLEET_ADMIN_PASSWORD (idempotent)."

    def handle(self, *args, **o):
        user = os.environ.get("FLEET_ADMIN_USER", "admin")
        pw = os.environ.get("FLEET_ADMIN_PASSWORD")
        if not pw:
            self.stdout.write("FLEET_ADMIN_PASSWORD not set - skipping admin creation.")
            return
        U = get_user_model()
        u, created = U.objects.get_or_create(username=user, defaults={"is_staff": True, "is_superuser": True})
        if created:
            u.set_password(pw)
            u.save()
            self.stdout.write(self.style.SUCCESS(f"Admin user '{user}' created."))
        else:
            self.stdout.write(f"Admin user '{user}' already exists (password unchanged).")
