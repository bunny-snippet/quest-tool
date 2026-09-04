from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import EmployeeProfile
from vendors.models import Client, OrganizationUnit

from .models import FinalIDStatus, FinalIDUpload, Survey, SurveyAttempt


class UserDashboardTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="performance-admin",
            email="performance-admin@example.test",
            password="test-password",
        )
        self.employee = get_user_model().objects.create_user(
            username="monthly-worker",
            first_name="Monthly",
            last_name="Worker",
            email="worker@example.test",
        )
        self.branch = OrganizationUnit.objects.create(
            workspace_owner=self.admin,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Noida",
            code="noida",
            created_by=self.admin,
        )
        self.sub_branch = OrganizationUnit.objects.create(
            workspace_owner=self.admin,
            parent=self.branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Quantish Noida",
            code="quantish-noida",
            created_by=self.admin,
        )
        self.shift = OrganizationUnit.objects.create(
            workspace_owner=self.admin,
            parent=self.sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning",
            code="morning",
            created_by=self.admin,
        )
        EmployeeProfile.objects.filter(user=self.employee).update(
            organization_unit=self.shift,
            created_by=self.admin,
        )
        self.client_record = Client.objects.create(code="performance-client", name="Performance Client")
        self.survey = Survey.objects.create(
            client=self.client_record,
            source_id=9001,
            source_key="9001",
            company_name="Performance Client",
            country_code="US",
            country="United States",
            buyer_id="buyer-9001",
        )
        now = timezone.now()
        self.attempts = []
        for index in range(4):
            status = SurveyAttempt.Status.COMPLETED if index < 3 else SurveyAttempt.Status.TERMINATED
            self.attempts.append(SurveyAttempt.objects.create(
                rid=f"DashRID{index:02d}",
                survey=self.survey,
                platform_user=self.employee,
                user_id=str(self.employee.pk),
                status=status,
                initiated_at=now,
            ))
        accounting_month = date(timezone.localdate().year, timezone.localdate().month, 1)
        accepted_upload = FinalIDUpload.objects.create(
            client=self.client_record,
            accounting_month=accounting_month,
            decision=FinalIDUpload.Decision.ACCEPTED,
            original_filename="accepted.csv",
            file_sha256="a" * 64,
            uploaded_by=self.admin,
        )
        rejected_upload = FinalIDUpload.objects.create(
            client=self.client_record,
            accounting_month=accounting_month,
            decision=FinalIDUpload.Decision.REJECTED,
            original_filename="rejected.csv",
            file_sha256="b" * 64,
            uploaded_by=self.admin,
        )
        FinalIDStatus.objects.create(
            attempt=self.attempts[0], client=self.client_record,
            status=FinalIDUpload.Decision.ACCEPTED,
            accounting_month=accounting_month, upload=accepted_upload,
        )
        FinalIDStatus.objects.create(
            attempt=self.attempts[1], client=self.client_record,
            status=FinalIDUpload.Decision.REJECTED,
            accounting_month=accounting_month, upload=rejected_upload,
        )

    def test_super_admin_dashboard_uses_latest_final_id_status(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        today = timezone.localdate()
        response = api.get(reverse("user-dashboard-api"), {
            "month": today.month,
            "year": today.year,
            "user": self.employee.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["user_id"], self.employee.pk)
        self.assertEqual(row["branch"], "Noida")
        self.assertEqual(row["sub_branch"], "Quantish Noida")
        self.assertEqual(row["shift"], "Morning")
        self.assertEqual(row["completes"], 3)
        self.assertEqual(row["accepted"], 1)
        self.assertEqual(row["rejected"], 1)
        self.assertEqual(row["pending"], 1)
        self.assertEqual(row["acceptance_rate"], 33.3)
        self.assertEqual(row["rejection_rate"], 33.3)
        self.assertEqual(row["pending_rate"], 33.3)
        self.assertEqual(row["reviewed_rate"], 66.7)

    def test_dashboard_defaults_to_overall_instead_of_partial_current_month(self):
        older = timezone.now().replace(year=timezone.now().year - 1)
        SurveyAttempt.objects.create(
            rid="OlderRID01",
            survey=self.survey,
            platform_user=self.employee,
            user_id=str(self.employee.pk),
            status=SurveyAttempt.Status.COMPLETED,
            initiated_at=older,
        )
        api = APIClient()
        api.force_authenticate(self.admin)
        response = api.get(reverse("user-dashboard-api"), {"user": self.employee.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["period"]["label"], "Overall")
        self.assertEqual(response.data["results"][0]["completes"], 4)

    def test_hierarchy_filter_and_page_are_available_to_super_admin(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        today = timezone.localdate()
        response = api.get(reverse("user-dashboard-api"), {
            "month": today.month,
            "year": today.year,
            "branch": self.branch.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["user_id"] for row in response.data["results"]}, {self.employee.pk})

        self.client.force_login(self.admin)
        page = self.client.get(reverse("user-dashboard"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "User Dashboard")
        self.assertContains(page, "user_dashboard.js")
        self.assertContains(page, "Quantish Noida")
        self.assertContains(page, "userDashboardFromDateTime")
        self.assertContains(page, "data-user-dashboard-filter=\"client\"")
        self.assertContains(page, "data-user-dashboard-filter=\"final_status\"")
        self.assertNotContains(page, "userDashboardShift")
        self.assertContains(page, 'id="exportUserDashboard"')

    def test_filtered_dashboard_export_builds_an_excel_workbook(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("user-dashboard-export"), {
            "user": self.employee.pk,
            "client": self.client_record.pk,
            "country": "US",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("user-dashboard-", response["Content-Disposition"])
        self.assertIn(".xlsx", response["Content-Disposition"])
        self.assertIsNotNone(getattr(response, "_export_workbook", None))

    def test_user_dashboard_export_can_be_queued(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("export-job-create", kwargs={"kind": "user_dashboard"}),
            {"user": self.employee.pk},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_client_country_buyer_and_final_status_filters_are_applied(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        today = timezone.localdate()
        response = api.get(reverse("user-dashboard-api"), {
            "month": today.month,
            "year": today.year,
            "client": self.client_record.pk,
            "country": "US",
            "buyer_id": "buyer-9001",
            "final_status": "accepted",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["completes"], 1)
        self.assertEqual(response.data["results"][0]["accepted"], 1)
        self.assertEqual(response.data["results"][0]["rejected"], 0)

    def test_employee_without_permission_cannot_open_dashboard(self):
        api = APIClient()
        api.force_authenticate(self.employee)
        self.assertEqual(api.get(reverse("user-dashboard-api")).status_code, 403)
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("user-dashboard")).status_code, 403)
