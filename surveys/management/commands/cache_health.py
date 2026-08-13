from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from config.cache_utils import jittered_ttl


class Command(BaseCommand):
    help = "Verify the configured Django cache with a short-lived write/read/delete probe."

    def handle(self, *args, **options):
        key = "health:management-command"
        value = "ok"
        try:
            cache.set(key, value, timeout=15)
            observed = cache.get(key)
            cache.delete(key)
        except Exception as exc:
            raise CommandError(f"Cache probe failed: {exc}") from exc
        if observed != value:
            raise CommandError("Cache probe failed: the value read did not match the value written.")
        backend = settings.CACHES["default"]["BACKEND"]
        location = settings.CACHES["default"].get("LOCATION", "")
        if location.startswith("redis://") and "@" in location:
            location = "redis://***@" + location.split("@", 1)[1]
        self.stdout.write(self.style.SUCCESS(
            "Cache healthy. "
            f"backend={backend} location={location or 'in-process'} "
            f"default_ttl={settings.CACHE_DEFAULT_TTL_SECONDS}s "
            f"sample_jittered_ttl={jittered_ttl()}s"
        ))
