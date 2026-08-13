"""Small fail-open helpers for non-authoritative Django cache data."""

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)
_MISSING = object()


def jittered_ttl(base_seconds: int | None = None, jitter_seconds: int | None = None) -> int:
    """Return a positive TTL spread around the configured base.

    The random spread prevents a large group of related keys from expiring in
    the same second and stampeding the database.
    """

    base = max(1, int(base_seconds or settings.CACHE_DEFAULT_TTL_SECONDS))
    jitter = max(
        0,
        int(
            settings.CACHE_TTL_JITTER_SECONDS
            if jitter_seconds is None
            else jitter_seconds
        ),
    )
    if not jitter:
        return base
    return max(1, base - jitter + secrets.randbelow((jitter * 2) + 1))


def stable_cache_key(namespace: str, value: Any = None) -> str:
    """Build a bounded key without exposing filter values in Redis key names."""

    if value is None:
        return namespace
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


def safe_cache_get(key: str, default: Any = None) -> Any:
    try:
        return cache.get(key, default)
    except Exception:
        logger.warning("Cache read failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return default


def safe_cache_set(key: str, value: Any, *, timeout: int | None = None) -> bool:
    try:
        cache.set(key, value, timeout=jittered_ttl(timeout))
        return True
    except Exception:
        logger.warning("Cache write failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return False


def safe_cache_delete(key: str) -> bool:
    try:
        return bool(cache.delete(key))
    except Exception:
        logger.warning("Cache delete failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return False


def safe_cache_get_or_set(
    key: str,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
) -> Any:
    cached = safe_cache_get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    value = factory()
    safe_cache_set(key, value, timeout=timeout)
    return value


def safe_cache_increment(key: str, *, default: int = 1) -> int:
    """Increment a namespace version without making cache availability critical."""

    try:
        cache.add(key, default, timeout=None)
        return int(cache.incr(key))
    except ValueError:
        try:
            cache.set(key, default + 1, timeout=None)
        except Exception:
            logger.warning(
                "Cache version reset failed for key namespace=%s",
                key.split(":", 1)[0],
                exc_info=True,
            )
        return default + 1
    except Exception:
        logger.warning("Cache increment failed for key namespace=%s", key.split(":", 1)[0], exc_info=True)
        return default
