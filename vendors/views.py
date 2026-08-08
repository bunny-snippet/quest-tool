from django.contrib.auth import get_user_model
from django.db.models import Count
from django.shortcuts import render
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.access import HasFunctionPermission, any_function_permission_required, has_function_access
from accounts.models import EmployeeProfile

from .access import vendor_scope_user_id

from .models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)
from .serializers import (
    AllocationReservationSerializer,
    ClientIntegrationSerializer,
    ClientSerializer,
    VendorClientAllocationSerializer,
    VendorCommercialProfileSerializer,
    VendorSurveyAllocationSerializer,
    VendorDirectorySerializer,
    VendorManagementOptionsSerializer,
)


@any_function_permission_required("vendors.view", "vendors.manage", "allocations.view", "allocations.manage")
def vendor_management_page(request):
    can_view_vendors = any(has_function_access(request.user, code) for code in ("vendors.view", "vendors.manage"))
    can_view_allocations = any(has_function_access(request.user, code) for code in ("allocations.view", "allocations.manage"))
    return render(request, "vendors/management.html", {
        "active_page": "vendors",
        "can_view_vendors": can_view_vendors,
        "can_view_allocations": can_view_allocations,
        "can_manage_vendors": has_function_access(request.user, "vendors.manage"),
        "can_manage_allocations": has_function_access(request.user, "allocations.manage"),
    })


class VendorManagementOptionsView(APIView):
    """Small non-secret lookup lists used by the vendor allocation workspace."""

    permission_classes = [HasFunctionPermission]
    required_function_permission = (
        "vendors.view", "vendors.manage", "allocations.view", "allocations.manage",
    )

    @extend_schema(
        tags=["Vendors & allocations"],
        summary="List safe vendor-management selector options",
        description="Returns non-secret active vendor and client labels for allocation modals, scoped to the current vendor hierarchy when applicable.",
        responses={200: VendorManagementOptionsSerializer},
    )
    def get(self, request):
        vendor_id = vendor_scope_user_id(request.user)
        vendors = get_user_model().objects.filter(
            employee_profile__account_type__in={
                EmployeeProfile.AccountType.INTERNAL_VENDOR,
                EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            },
            is_active=True,
        ).select_related("employee_profile").order_by("first_name", "last_name", "username")
        clients = Client.objects.filter(is_active=True).order_by("name")
        if vendor_id:
            vendors = vendors.filter(pk=vendor_id)
            clients = clients.filter(vendor_allocations__vendor_id=vendor_id).distinct()
        return Response({
            "vendors": [
                {
                    "id": vendor.pk,
                    "full_name": vendor.get_full_name() or vendor.username,
                    "username": vendor.username,
                    "account_type": vendor.employee_profile.account_type,
                }
                for vendor in vendors
            ],
            "clients": [
                {"id": client.pk, "name": client.name, "code": client.code, "provider_code": client.provider_code}
                for client in clients
            ],
        })


class VendorScopedQuerysetMixin:
    vendor_scope_filter = None

    def get_queryset(self):
        queryset = super().get_queryset()
        vendor_id = vendor_scope_user_id(self.request.user)
        if vendor_id and self.vendor_scope_filter:
            queryset = queryset.filter(**{self.vendor_scope_filter: vendor_id}).distinct()
        return queryset


class PermissionByActionMixin(VendorScopedQuerysetMixin):
    view_permission = None
    manage_permission = None

    def get_required_function_permission(self):
        return (self.view_permission, self.manage_permission) if self.action in {"list", "retrieve"} else self.manage_permission

    def perform_create(self, serializer):
        if vendor_scope_user_id(self.request.user):
            raise PermissionDenied("Vendor-scoped accounts cannot change owner-controlled commercial data.")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        if vendor_scope_user_id(self.request.user):
            raise PermissionDenied("Vendor-scoped accounts cannot change owner-controlled commercial data.")
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        if vendor_scope_user_id(request.user):
            raise PermissionDenied("Vendor-scoped accounts cannot change owner-controlled commercial data.")
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List internal and external vendor accounts"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get a vendor account and commercial summary"),
)
class VendorDirectoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = VendorDirectorySerializer
    permission_classes = [HasFunctionPermission]
    required_function_permission = ("vendors.view", "vendors.manage")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["employee_profile__account_type", "is_active"]
    search_fields = ["username", "first_name", "last_name", "email"]
    ordering_fields = ["first_name", "last_name", "date_joined"]
    ordering = ["first_name", "last_name", "username"]

    def get_queryset(self):
        queryset = get_user_model().objects.filter(
            employee_profile__account_type__in={
                EmployeeProfile.AccountType.INTERNAL_VENDOR,
                EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            }
        ).select_related(
            "employee_profile", "employee_profile__role", "employee_profile__created_by",
            "vendor_commercial_profile",
        ).annotate(allocation_count=Count("client_allocations", distinct=True))
        vendor_id = vendor_scope_user_id(self.request.user)
        return queryset.filter(pk=vendor_id) if vendor_id else queryset


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List survey clients"),
    create=extend_schema(tags=["Vendors & allocations"], summary="Create a survey client"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get a survey client"),
    update=extend_schema(tags=["Vendors & allocations"], summary="Replace a survey client"),
    partial_update=extend_schema(tags=["Vendors & allocations"], summary="Update a survey client"),
    destroy=extend_schema(tags=["Vendors & allocations"], summary="Deactivate a survey client"),
)
class ClientViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = Client.objects.select_related("created_by").prefetch_related("integrations").all()
    serializer_class = ClientSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "clients.view"
    manage_permission = "clients.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["provider_code", "is_active"]
    search_fields = ["name", "code", "company_name_match"]
    ordering_fields = ["name", "created_at", "updated_at"]
    ordering = ["name"]
    vendor_scope_filter = "vendor_allocations__vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List non-secret client integration metadata"),
    create=extend_schema(tags=["Vendors & allocations"], summary="Create client integration metadata"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get client integration metadata"),
    update=extend_schema(tags=["Vendors & allocations"], summary="Replace client integration metadata"),
    partial_update=extend_schema(tags=["Vendors & allocations"], summary="Update client integration metadata"),
    destroy=extend_schema(tags=["Vendors & allocations"], summary="Deactivate client integration metadata"),
)
class ClientIntegrationViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = ClientIntegration.objects.select_related("client", "created_by").all()
    serializer_class = ClientIntegrationSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "clients.view"
    manage_permission = "clients.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["client", "provider_code", "scheduled_sync_enabled", "is_active"]
    search_fields = ["name", "client__name", "client__code"]
    vendor_scope_filter = "client__vendor_allocations__vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List vendor CPI policies"),
    create=extend_schema(tags=["Vendors & allocations"], summary="Create a vendor CPI policy"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get a vendor CPI policy"),
    update=extend_schema(tags=["Vendors & allocations"], summary="Replace a vendor CPI policy"),
    partial_update=extend_schema(tags=["Vendors & allocations"], summary="Update a vendor CPI policy"),
    destroy=extend_schema(tags=["Vendors & allocations"], summary="Deactivate a vendor CPI policy"),
)
class VendorCommercialProfileViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = VendorCommercialProfile.objects.select_related(
        "vendor", "vendor__employee_profile", "created_by"
    ).all()
    serializer_class = VendorCommercialProfileSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "vendors.view"
    manage_permission = "vendors.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["vendor", "vendor__employee_profile__account_type", "is_active"]
    search_fields = ["vendor__username", "vendor__first_name", "vendor__last_name", "vendor__email"]
    vendor_scope_filter = "vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List vendor client grants and quantities"),
    create=extend_schema(tags=["Vendors & allocations"], summary="Allocate a client and quantity to a vendor"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get a vendor client allocation"),
    update=extend_schema(tags=["Vendors & allocations"], summary="Replace a vendor client allocation"),
    partial_update=extend_schema(tags=["Vendors & allocations"], summary="Update a vendor client allocation"),
    destroy=extend_schema(tags=["Vendors & allocations"], summary="Deactivate a vendor client allocation"),
)
class VendorClientAllocationViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = VendorClientAllocation.objects.select_related(
        "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile", "client", "created_by"
    ).all()
    serializer_class = VendorClientAllocationSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "allocations.view"
    manage_permission = "allocations.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["vendor", "client", "vendor__employee_profile__account_type", "is_active"]
    search_fields = ["vendor__username", "vendor__first_name", "vendor__last_name", "client__name", "client__code"]
    ordering_fields = ["created_at", "updated_at", "quantity_limit", "consumed_quantity"]
    ordering = ["client__name", "vendor__username"]
    vendor_scope_filter = "vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List optional vendor survey overrides"),
    create=extend_schema(tags=["Vendors & allocations"], summary="Create a survey-specific quantity or CPI override"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get a survey allocation override"),
    update=extend_schema(tags=["Vendors & allocations"], summary="Replace a survey allocation override"),
    partial_update=extend_schema(tags=["Vendors & allocations"], summary="Update a survey allocation override"),
    destroy=extend_schema(tags=["Vendors & allocations"], summary="Deactivate a survey allocation override"),
)
class VendorSurveyAllocationViewSet(PermissionByActionMixin, viewsets.ModelViewSet):
    queryset = VendorSurveyAllocation.objects.select_related(
        "client_allocation", "client_allocation__vendor", "client_allocation__vendor__employee_profile",
        "client_allocation__vendor__vendor_commercial_profile", "client_allocation__client", "survey", "created_by",
    ).all()
    serializer_class = VendorSurveyAllocationSerializer
    permission_classes = [HasFunctionPermission]
    view_permission = "allocations.view"
    manage_permission = "allocations.manage"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["client_allocation", "client_allocation__vendor", "client_allocation__client", "survey", "is_active"]
    search_fields = [
        "client_allocation__vendor__username", "client_allocation__vendor__first_name",
        "client_allocation__vendor__last_name", "survey__local_id", "survey__source_id", "survey__name",
    ]
    ordering_fields = ["created_at", "updated_at", "quantity_limit", "consumed_quantity"]
    ordering = ["survey__source_id", "client_allocation__vendor__username"]
    vendor_scope_filter = "client_allocation__vendor_id"


@extend_schema_view(
    list=extend_schema(tags=["Vendors & allocations"], summary="List allocation reservation audit records"),
    retrieve=extend_schema(tags=["Vendors & allocations"], summary="Get an allocation reservation audit record"),
)
class AllocationReservationViewSet(VendorScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = AllocationReservation.objects.select_related(
        "attempt", "client_allocation", "client_allocation__vendor", "survey_allocation", "survey_allocation__survey",
    ).all()
    serializer_class = AllocationReservationSerializer
    permission_classes = [HasFunctionPermission]
    required_function_permission = ("allocations.view", "allocations.manage")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "client_allocation", "client_allocation__vendor", "survey_allocation"]
    search_fields = ["attempt__rid", "client_allocation__vendor__username", "survey_allocation__survey__local_id"]
    ordering_fields = ["created_at", "expires_at", "finalized_at", "status"]
    ordering = ["-created_at"]
    vendor_scope_filter = "client_allocation__vendor_id"
