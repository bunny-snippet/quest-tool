from types import SimpleNamespace

from django.test import SimpleTestCase

from .outcomes import termination_origin


class TerminationOriginTests(SimpleTestCase):
    def _attempt(self, status_source, provider_code="biobrain"):
        return SimpleNamespace(
            status_source=status_source,
            survey=SimpleNamespace(
                integration=SimpleNamespace(provider_code=provider_code),
            ),
        )

    def test_local_pre_screener_termination_is_not_presented_as_client_end(self):
        origin = termination_origin(self._attempt("local_duplicate_ip_guard"))

        self.assertEqual(origin["location"], "prescreener")
        self.assertEqual(origin["label"], "Pre-screener ended")
        self.assertIn("duplicate-IP", origin["detail"])

    def test_biobrain_browser_return_is_presented_as_client_end(self):
        origin = termination_origin(self._attempt("browser_callback"))

        self.assertEqual(origin["location"], "client")
        self.assertEqual(origin["label"], "Client / provider ended")
        self.assertIn("Biobrain reported", origin["detail"])
