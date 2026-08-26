from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.db.models import F
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import AccessFunction, EmployeeProfile, Role, UserFunctionOverride
from surveys import project_cache
from surveys.models import Survey
from surveys.project_cache import project_filter_metadata
from vendors.models import Client, VendorClientAllocation, VendorCommercialProfile


class ProjectCacheSingleflightTests(SimpleTestCase):
    def test_peer_uses_value_published_by_the_lock_owner(self):
        published = {"countries": [("US", "United States")]}
        factory_calls = []

        with (
            patch.object(
                project_cache,
                "safe_cache_get",
                side_effect=[project_cache._MISSING, published],
            ),
            patch.object(project_cache, "safe_cache_add", return_value=False),
            patch.object(project_cache.time, "monotonic", side_effect=[0, 0]),
            patch.object(project_cache.time, "sleep") as sleep,
        ):
            result = project_cache._singleflight_get_or_set(
                "projects:test",
                lambda: factory_calls.append(True),
                timeout=600,
                jitter_seconds=0,
            )

        self.assertEqual(result, published)
        self.assertEqual(factory_calls, [])
        sleep.assert_called_once_with(0.05)

    def test_lock_owner_publishes_and_releases_only_its_token(self):
        with (
            patch.object(
                project_cache,
                "safe_cache_get",
                return_value=project_cache._MISSING,
            ),
            patch.object(project_cache, "safe_cache_add", return_value=True),
            patch.object(project_cache, "safe_cache_set", return_value=True) as cache_set,
            patch.object(
                project_cache,
                "safe_cache_compare_delete",
            ) as compare_delete,
        ):
            result = project_cache._singleflight_get_or_set(
                "projects:test",
                lambda: {"count": 12},
                timeout=90,
                jitter_seconds=0,
            )

        self.assertEqual(result, {"count": 12})
        cache_set.assert_called_once()
        compare_delete.assert_called_once()
        self.assertEqual(
            compare_delete.call_args.args[0],
            "projects:test:build-lock",
        )
        self.assertIsInstance(compare_delete.call_args.args[1], int)
        self.assertEqual(
            compare_delete.call_args.kwargs,
            {"alias": project_cache.CACHE_ALIAS},
        )


@override_settings(
    PROJECT_CACHE_FILTERS_TTL_SECONDS=600,
    PROJECT_CACHE_TTL_JITTER_SECONDS=0,
)
class ProjectMetadataQuerysetTests(TestCase):
    def setUp(self):
        caches["projects"].clear()
        self.addCleanup(caches["projects"].clear)
        self.user = get_user_model().objects.create_user(
            username="project-metadata-queryset",
        )

    def test_filter_dimensions_and_pricing_bounds_can_use_separate_querysets(self):
        visible = Survey.objects.create(
            source_id=910001,
            country_code="US",
            country="United States",
            company_name="Visible client",
            cpi="2.50",
        )
        priced_only = Survey.objects.create(
            source_id=910002,
            country_code="CA",
            country="Canada",
            company_name="Pricing client",
            cpi="7.00",
        )

        metadata = project_filter_metadata(
            Survey.objects.filter(pk=visible.pk),
            user_id=self.user.pk,
            client_scoped=False,
            include_cpi=True,
            cpi_field="visible_cpi",
            cpi_queryset=Survey.objects.filter(pk=priced_only.pk).annotate(
                visible_cpi=F("cpi")
            ),
        )

        self.assertEqual(metadata["countries"], [("US", "United States")])
        self.assertEqual(str(metadata["cpi_min"]), "7")
        self.assertEqual(str(metadata["cpi_max"]), "7")


class ProjectIndexContractTests(SimpleTestCase):
    def test_large_inventory_queries_have_matching_composite_indexes(self):
        index_names = {index.name for index in Survey._meta.indexes}

        self.assertIn("survey_modified_created_idx", index_names)
        self.assertIn("survey_country_label_idx", index_names)
        self.assertIn("survey_buyer_scope_idx", index_names)


class VendorScopedProjectCpiTests(TestCase):
    def setUp(self):
        caches["projects"].clear()
        self.addCleanup(caches["projects"].clear)
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            username="project-cpi-owner",
            password="test-password",
        )
        self.vendor = User.objects.create_user(
            username="project-cpi-vendor",
            password="test-password",
        )
        EmployeeProfile.objects.filter(user=self.vendor).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            role=Role.objects.get(slug="external-vendor"),
            created_by=self.owner,
        )
        self.vendor._state.fields_cache.pop("employee_profile", None)
        self.client_record = Client.objects.create(
            code="project-cpi-client",
            name="Project CPI client",
            provider_code="innovatemr",
            created_by=self.owner,
        )
        VendorCommercialProfile.objects.create(
            vendor=self.vendor,
            default_cpi_cut_percent=Decimal("30.00"),
            created_by=self.owner,
        )
        VendorClientAllocation.objects.create(
            vendor=self.vendor,
            client=self.client_record,
            quantity_limit=100,
            created_by=self.owner,
        )
        self.in_range = Survey.objects.create(
            client=self.client_record,
            source_id=920001,
            cpi=Decimal("10.00"),
            remaining=10,
            status=Survey.Status.LIVE,
        )
        self.above_range = Survey.objects.create(
            client=self.client_record,
            source_id=920002,
            cpi=Decimal("20.00"),
            remaining=10,
            status=Survey.Status.LIVE,
        )
        UserFunctionOverride.objects.create(
            user=self.vendor,
            function=AccessFunction.objects.get(code="projects.filter.cpi"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )

    def test_page_skips_correlated_bounds_but_api_cpi_filter_stays_exact(self):
        browser = self.client
        browser.force_login(self.vendor)
        empty_metadata = {
            "countries": [],
            "companies": [],
            "buyer_options": [],
            "survey_types": [],
            "cpi_min": None,
            "cpi_max": Decimal("175.00"),
        }
        with (
            patch("surveys.views.project_filter_metadata", return_value=empty_metadata) as metadata,
            patch("surveys.views.annotate_survey_pricing_for_user") as annotate_pricing,
        ):
            page = browser.get(reverse("projects"))

        self.assertEqual(page.status_code, 200)
        self.assertTrue(metadata.call_args.kwargs["include_cpi"])
        self.assertEqual(metadata.call_args.kwargs["cpi_field"], "cpi")
        annotate_pricing.assert_not_called()
        self.assertEqual(page.context["cpi_min_bound"], 0)
        self.assertEqual(page.context["cpi_max_bound"], Decimal("175.00"))

        api = APIClient()
        api.force_authenticate(self.vendor)
        filtered = api.get(
            reverse("survey-list"),
            {"min_cpi": "6.50", "max_cpi": "7.50", "ordering": "-cpi"},
        )

        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(
            [row["local_id"] for row in filtered.data["results"]],
            [self.in_range.local_id],
        )
        self.assertEqual(
            Decimal(str(filtered.data["results"][0]["cpi"])),
            Decimal("7.00"),
        )
