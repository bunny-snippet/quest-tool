from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from config.cache_utils import (
    jittered_ttl,
    safe_cache_get_or_set,
    stable_cache_key,
)


@override_settings(CACHE_DEFAULT_TTL_SECONDS=100, CACHE_TTL_JITTER_SECONDS=20)
class CacheUtilityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_jittered_ttl_stays_inside_configured_range(self):
        values = {jittered_ttl() for _ in range(100)}
        self.assertTrue(all(80 <= value <= 120 for value in values))
        self.assertGreater(len(values), 1)

    def test_stable_key_hides_filter_values_and_ignores_dict_order(self):
        first = stable_cache_key("vault", {"country": "US", "gender": "male"})
        second = stable_cache_key("vault", {"gender": "male", "country": "US"})
        self.assertEqual(first, second)
        self.assertNotIn("male", first)
        self.assertNotIn("US", first)

    def test_get_or_set_loads_once(self):
        calls = []

        def factory():
            calls.append(True)
            return {"value": 1}

        first = safe_cache_get_or_set("test:get-or-set", factory)
        second = safe_cache_get_or_set("test:get-or-set", factory)
        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})
        self.assertEqual(len(calls), 1)

    @patch("config.cache_utils.cache.get", side_effect=ConnectionError("redis unavailable"))
    @patch("config.cache_utils.cache.set", side_effect=ConnectionError("redis unavailable"))
    def test_cache_outage_falls_back_to_factory(self, cache_set, cache_get):
        self.assertEqual(
            safe_cache_get_or_set("test:outage", lambda: {"from": "database"}),
            {"from": "database"},
        )
        cache_get.assert_called_once()
        cache_set.assert_called_once()
