from django.conf import settings
from django.core.cache import caches
from django.core.management.base import BaseCommand, CommandError

from config.cache_utils import jittered_ttl


class Command(BaseCommand):
    help = "Verify the configured Django cache with a short-lived write/read/delete probe."

    def handle(self, *args, **options):
        for alias in ("default", "projects"):
            key = f"health:management-command:{alias}"
            value = "ok"
            try:
                backend_cache = caches[alias]
                backend_cache.set(key, value, timeout=15)
                observed = backend_cache.get(key)
                backend_cache.delete(key)
            except Exception as exc:
                raise CommandError(f"Cache probe failed for {alias}: {exc}") from exc
            if observed != value:
                raise CommandError(
                    f"Cache probe failed for {alias}: the value read did not match."
                )
            backend = settings.CACHES[alias]["BACKEND"]
            location = settings.CACHES[alias].get("LOCATION", "")
            if location.startswith("redis://") and "@" in location:
                location = "redis://***@" + location.split("@", 1)[1]
            ttl = (
                settings.PROJECT_CACHE_DEFAULT_TTL_SECONDS
                if alias == "projects"
                else settings.CACHE_DEFAULT_TTL_SECONDS
            )
            jitter = (
                settings.PROJECT_CACHE_TTL_JITTER_SECONDS
                if alias == "projects"
                else settings.CACHE_TTL_JITTER_SECONDS
            )
            self.stdout.write(self.style.SUCCESS(
                f"Cache healthy. alias={alias} backend={backend} "
                f"location={location or 'in-process'} default_ttl={ttl}s "
                f"sample_jittered_ttl={jittered_ttl(ttl, jitter)}s"
            ))
