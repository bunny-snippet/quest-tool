from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from config.filter_backends import SparseDjangoFilterBackend
from surveys.filters import SurveyAttemptFilter
from surveys.models import SurveyAttempt


class CountingSurveyAttemptFilter(SurveyAttemptFilter):
    initializations = 0

    def __init__(self, *args, **kwargs):
        type(self).initializations += 1
        super().__init__(*args, **kwargs)


class SparseDjangoFilterBackendTests(SimpleTestCase):
    def setUp(self):
        CountingSurveyAttemptFilter.initializations = 0
        self.factory = APIRequestFactory()
        self.backend = SparseDjangoFilterBackend()
        self.view = SimpleNamespace(filterset_class=CountingSurveyAttemptFilter)
        self.queryset = SurveyAttempt.objects.all()

    def request(self, query=""):
        return Request(self.factory.get(f"/api/v1/survey-attempts/?{query}"))

    def test_unfiltered_request_skips_filterset_construction(self):
        result = self.backend.filter_queryset(
            self.request("page=1&ordering=-initiated_at&unknown=value"),
            self.queryset,
            self.view,
        )

        self.assertIs(result, self.queryset)
        self.assertEqual(CountingSurveyAttemptFilter.initializations, 0)

    def test_declared_filter_keeps_normal_validation_and_filtering(self):
        result = self.backend.filter_queryset(
            self.request("status=2"),
            self.queryset,
            self.view,
        )

        self.assertEqual(CountingSurveyAttemptFilter.initializations, 1)
        self.assertIn('"surveys_surveyattempt"."status" IN (2)', str(result.query))
