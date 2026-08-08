from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from accounts.access import HasFunctionPermission

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
)


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
        return self.view_permission if self.action in {"list", "retrieve"} else self.manage_permission

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


class AllocationReservationViewSet(VendorScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = AllocationReservation.objects.select_related(
        "attempt", "client_allocation", "client_allocation__vendor", "survey_allocation", "survey_allocation__survey",
    ).all()
    serializer_class = AllocationReservationSerializer
    permission_classes = [HasFunctionPermission]
    required_function_permission = "allocations.view"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "client_allocation", "client_allocation__vendor", "survey_allocation"]
    search_fields = ["attempt__rid", "client_allocation__vendor__username", "survey_allocation__survey__local_id"]
    ordering_fields = ["created_at", "expires_at", "finalized_at", "status"]
    ordering = ["-created_at"]
    vendor_scope_filter = "client_allocation__vendor_id"
