from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .access import has_function_access
from .models import AccessFunction, EmployeeProfile, Role, UserFunctionOverride


class LoginAndSetupTests(TestCase):
    def test_anonymous_internal_page_redirects_to_login(self):
        response = self.client.get(reverse("projects"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_first_owner_setup_creates_super_admin_and_closes_setup(self):
        response = self.client.post(reverse("first-admin-setup"), {
            "first_name": "Workspace", "last_name": "Owner", "username": "owner",
            "email": "owner@example.test", "password1": "safe-password-123", "password2": "safe-password-123",
        })
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        user = get_user_model().objects.get(username="owner")
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.employee_profile.role.slug, "super-admin")
        self.assertEqual(self.client.get(reverse("first-admin-setup")).status_code, 404)


class FunctionAccessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="employee", password="password-123")
        self.client.force_login(self.user)

    def test_employee_role_can_view_projects_but_not_access_control(self):
        self.assertTrue(has_function_access(self.user, "projects.view"))
        self.assertEqual(self.client.get(reverse("projects")).status_code, 200)
        self.assertEqual(self.client.get(reverse("access-control")).status_code, 403)

    def test_user_allow_and_deny_override_role_baseline(self):
        attempts = AccessFunction.objects.get(code="attempts.view")
        projects = AccessFunction.objects.get(code="projects.view")
        UserFunctionOverride.objects.create(user=self.user, function=attempts, effect="allow")
        UserFunctionOverride.objects.create(user=self.user, function=projects, effect="deny")
        self.assertTrue(has_function_access(self.user, "attempts.view"))
        self.assertFalse(has_function_access(self.user, "projects.view"))
        self.assertEqual(self.client.get(reverse("projects")).status_code, 403)
        self.assertRedirects(self.client.get(reverse("home")), reverse("dashboard"), fetch_redirect_response=False)

    def test_denied_navigation_and_project_column_are_not_rendered(self):
        UserFunctionOverride.objects.create(
            user=self.user, function=AccessFunction.objects.get(code="dashboard.view"), effect="deny"
        )
        UserFunctionOverride.objects.create(
            user=self.user, function=AccessFunction.objects.get(code="projects.column.cpi"), effect="deny"
        )
        response = self.client.get(reverse("projects"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{reverse("dashboard")}"')
        self.assertNotContains(response, "<th>CPI</th>", html=True)
        self.assertContains(response, "<th>Market</th>", html=True)
        self.assertNotContains(response, 'id="syncButton"')

    def test_employee_cannot_call_protected_tracking_api(self):
        response = APIClient().get(reverse("survey-attempt-list"))
        self.assertIn(response.status_code, {401, 403})
        api = APIClient()
        api.force_authenticate(self.user)
        self.assertEqual(api.get(reverse("survey-attempt-list")).status_code, 403)

    def test_super_admin_can_crud_role_permissions(self):
        owner = get_user_model().objects.create_superuser(username="owner", password="password-123")
        api = APIClient()
        api.force_authenticate(owner)
        response = api.post(reverse("access-role-list"), {
            "name": "Recruiter", "slug": "recruiter", "rank": 15,
            "permission_codes": ["projects.view", "survey_links.copy"],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        role = Role.objects.get(slug="recruiter")
        self.assertEqual(set(role.function_assignments.values_list("function__code", flat=True)), {"projects.view", "survey_links.copy"})

        self.client.force_login(owner)
        page = self.client.get(reverse("access-control"))
        self.assertContains(page, "Add user")
        self.assertContains(page, "userModal")


class DelegatedVendorTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(username="owner", email="owner@example.test", password="password-123")
        self.vendor = get_user_model().objects.create_user(username="vendor@example.test", email="vendor@example.test", password="password-123")
        self.vendor.employee_profile.created_by = self.owner
        self.vendor.employee_profile.account_type = EmployeeProfile.AccountType.EXTERNAL_VENDOR
        self.vendor.employee_profile.save()
        for code in ["permissions.view", "roles.view", "roles.create", "roles.update", "roles.delete", "users.view", "users.create", "users.update", "users.delete"]:
            UserFunctionOverride.objects.create(
                user=self.vendor, function=AccessFunction.objects.get(code=code), effect=UserFunctionOverride.Effect.ALLOW
            )
        self.api = APIClient()
        self.api.force_authenticate(self.vendor)

    def test_vendor_can_create_scoped_role_and_subordinate_user(self):
        role_response = self.api.post(reverse("access-role-list"), {
            "name": "Vendor operator", "slug": "vendor-operator", "rank": 12,
            "permission_codes": ["projects.view", "survey_details.view"],
        }, format="json")
        self.assertEqual(role_response.status_code, 201)
        self.assertEqual(Role.objects.get(slug="vendor-operator").created_by, self.vendor)

        user_response = self.api.post(reverse("access-user-list"), {
            "first_name": "Nested", "last_name": "Employee", "email": "nested@example.test",
            "password": "password-123", "role": "vendor-operator", "account_type": "internal_vendor",
            "company_name": "Nested Vendor", "allow_codes": [], "deny_codes": [],
        }, format="json")
        self.assertEqual(user_response.status_code, 201)
        nested = get_user_model().objects.get(email="nested@example.test")
        self.assertEqual(nested.employee_profile.created_by, self.vendor)
        self.assertEqual(nested.employee_profile.account_type, EmployeeProfile.AccountType.INTERNAL_VENDOR)

    def test_vendor_cannot_delegate_permission_it_does_not_have(self):
        response = self.api.post(reverse("access-role-list"), {
            "name": "Escalated", "slug": "escalated", "rank": 99,
            "permission_codes": ["sync.run"],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot delegate", str(response.data).lower())

    def test_vendor_cannot_see_sibling_vendor(self):
        sibling = get_user_model().objects.create_user(username="sibling", email="sibling@example.test")
        sibling.employee_profile.created_by = self.owner
        sibling.employee_profile.save()
        response = self.api.get(reverse("access-user-list"))
        self.assertEqual(response.status_code, 200)
        usernames = {item["username"] for item in response.data["results"]}
        self.assertNotIn("sibling", usernames)
