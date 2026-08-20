"""Request-local memoization for repeated access and hierarchy lookups.

The cache exists only for the lifetime of one HTTP request.  It therefore
removes duplicate permission queries across decorators, views, serializers and
template context processors without making permission changes eventually
consistent or dependent on Redis.
"""

from contextvars import ContextVar
from typing import Any, Callable


_request_cache: ContextVar[dict | None] = ContextVar(
    "workspace_request_cache",
    default=None,
)


def request_cached(key: tuple, factory: Callable[[], Any]) -> Any:
    cache = _request_cache.get()
    if cache is None:
        return factory()
    if key not in cache:
        cache[key] = factory()
    return cache[key]


class RequestAccessCacheMiddleware:
    """Create and reliably clear one memoization dictionary per request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request_cache.set({})
        try:
            return self.get_response(request)
        finally:
            _request_cache.reset(token)
