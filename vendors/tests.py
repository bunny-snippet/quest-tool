from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AccessFunction, EmployeeProfile, UserFunctionOverride
from surveys.models import Survey, SurveyAttempt

from .models import AllocationReservation, Client, VendorClientAllocation, VendorCommercialProfile, VendorSurveyAllocation
from .services import AllocationUnavailable, finalize_attempt_capacity, payable_cpi, reserve_attempt_capacity


class VendorFoundationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("vendor-owner", "owner@example.test", "test-password")
        self.internal = User.objects.create_user("internal-vendor", first_name="Internal")
        self.external = User.objects.create_user("external-vendor", first_name="External")
        self.employee = User.objects.create_user("ordinary-employee")
        EmployeeProfile.objects.filter(user=self.internal).update(
            account_type=EmployeeProfile.AccountType.INTERNAL_VENDOR,
            created_by=self.owner,
        )
        EmployeeProfile.objects.filter(user=self.external).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            created_by=self.owner,
        )
        self.client_record = Client.objects.create(
            code="uat-client",
            name="UAT Client",
            provider_code="innovatemr",
            created_by=self.owner,
        )
        self.survey = Survey.objects.create(
            client=self.client_record,
            source_id=88001,
            name="Allocation test survey",
            status=Survey.Status.LIVE,
            remaining=20,
            cpi=Decimal("10.00"),
        )
        self.external_policy = VendorCommercialProfile.objects.create(
            vendor=self.external,
            default_cpi_cut_percent=Decimal("30.00"),
            created_by=self.owner,
        )
        self.external_client_allocation = VendorClientAllocation.objects.create(
            vendor=self.external,
            client=self.client_record,
            quantity_limit=5,
            created_by=self.owner,
        )
        self.external_survey_allocation = VendorSurveyAllocation.objects.create(
            client_allocation=self.external_client_allocation,
            survey=self.survey,
            quantity_limit=2,
            created_by=self.owner,
        )

    def attempt(self, rid, status=SurveyAttempt.Status.INITIATED):
        return SurveyAttempt.objects.create(
            rid=rid,
            survey=self.survey,
            platform_user=self.external,
            user_id=str(self.external.pk),
            status=status,
        )

    def test_external_cut_and_internal_full_cpi_rules(self):
        self.assertEqual(payable_cpi(Decimal("10.00"), Decimal("30.00")), Decimal("7.00"))
        self.assertEqual(self.external_survey_allocation.effective_cpi_cut_percent, Decimal("30.00"))

        internal_policy = VendorCommercialProfile(
            vendor=self.internal,
            default_cpi_cut_percent=Decimal("1.00"),
            created_by=self.owner,
        )
        with self.assertRaises(ValidationError):
            internal_policy.full_clean()

    def test_reservation_freezes_cpi_and_completion_consumes_both_limits(self):
        attempt = self.attempt("Ua1Bb2Cc3D")
        reservation = reserve_attempt_capacity(attempt, self.external_survey_allocation)
        self.assertEqual(reservation.status, AllocationReservation.Status.RESERVED)
        attempt.refresh_from_db()
        self.assertEqual(attempt.vendor, self.external)
        self.assertEqual(attempt.client, self.client_record)
        self.assertEqual(attempt.source_cpi_snapshot, Decimal("10.00"))
        self.assertEqual(attempt.cpi_cut_percent_snapshot, Decimal("30.00"))
        self.assertEqual(attempt.payable_cpi_snapshot, Decimal("7.00"))

        self.survey.cpi = Decimal("6.00")
        self.survey.save(update_fields=["cpi"])
        attempt.status = SurveyAttempt.Status.COMPLETED
        attempt.save(update_fields=["status"])
        finalized = finalize_attempt_capacity(attempt)
        self.assertEqual(finalized.status, AllocationReservation.Status.CONSUMED)
        attempt.refresh_from_db()
        self.assertEqual(attempt.payable_cpi_snapshot, Decimal("7.00"))
        self.external_client_allocation.refresh_from_db()
        self.external_survey_allocation.refresh_from_db()
        self.assertEqual(self.external_client_allocation.consumed_quantity, 1)
        self.assertEqual(self.external_survey_allocation.consumed_quantity, 1)
        self.assertEqual(finalize_attempt_capacity(attempt).status, AllocationReservation.Status.CONSUMED)

    def test_non_complete_releases_and_exhausted_survey_rejects(self):
        attempt = self.attempt("Ua4Ee5Ff6G")
        reserve_attempt_capacity(attempt, self.external_survey_allocation)
        attempt.status = SurveyAttempt.Status.TERMINATED
        attempt.save(update_fields=["status"])
        self.assertEqual(finalize_attempt_capacity(attempt).status, AllocationReservation.Status.RELEASED)
        self.external_survey_allocation.refresh_from_db()
        self.assertEqual(self.external_survey_allocation.remaining_quantity, 2)

        self.external_survey_allocation.quantity_limit = 0
        self.external_survey_allocation.save(update_fields=["quantity_limit"])
        with self.assertRaisesMessage(AllocationUnavailable, "Survey quantity is exhausted"):
            reserve_attempt_capacity(self.attempt("Ua7Hh8Ii9J"), self.external_survey_allocation)

    def test_superuser_can_manage_foundation_api_and_employee_cannot(self):
        owner_api = APIClient()
        owner_api.force_authenticate(self.owner)
        response = owner_api.post(reverse("vendor-client-list"), {
            "code": "second-client",
            "name": "Second Client",
            "provider_code": "custom",
        })
        self.assertEqual(response.status_code, 201)

        employee_api = APIClient()
        employee_api.force_authenticate(self.employee)
        self.assertEqual(employee_api.get(reverse("vendor-client-list")).status_code, 403)

    def test_vendor_api_is_read_only_and_scoped_to_its_own_allocations(self):
        sibling = get_user_model().objects.create_user("sibling-vendor")
        EmployeeProfile.objects.filter(user=sibling).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            created_by=self.owner,
        )
        sibling_allocation = VendorClientAllocation.objects.create(
            vendor=sibling,
            client=self.client_record,
            quantity_limit=3,
            created_by=self.owner,
        )
        for code in ["clients.view", "vendors.view", "allocations.view", "allocations.manage"]:
            UserFunctionOverride.objects.create(
                user=self.external,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )

        api = APIClient()
        api.force_authenticate(self.external)
        listing = api.get(reverse("vendor-client-allocation-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["id"] for item in listing.data["results"]], [self.external_client_allocation.id])
        self.assertEqual(
            api.get(reverse("vendor-client-allocation-detail", kwargs={"pk": sibling_allocation.pk})).status_code,
            404,
        )
        self.assertEqual(
            api.patch(
                reverse("vendor-client-allocation-detail", kwargs={"pk": self.external_client_allocation.pk}),
                {"quantity_limit": 99},
                format="json",
            ).status_code,
            403,
        )

    def test_expiry_boundary_is_recorded_for_future_cleanup_job(self):
        attempt = self.attempt("Ua0Kk1Ll2M")
        reservation = reserve_attempt_capacity(
            attempt,
            self.external_survey_allocation,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        self.assertGreater(reservation.expires_at, timezone.now())
