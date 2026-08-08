from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AllocationReservationViewSet,
    ClientIntegrationViewSet,
    ClientViewSet,
    VendorClientAllocationViewSet,
    VendorCommercialProfileViewSet,
    VendorSurveyAllocationViewSet,
    VendorDirectoryViewSet,
    VendorManagementOptionsView,
)


router = DefaultRouter()
router.register("directory", VendorDirectoryViewSet, basename="vendor-directory")
router.register("clients", ClientViewSet, basename="vendor-client")
router.register("integrations", ClientIntegrationViewSet, basename="client-integration")
router.register("commercial-profiles", VendorCommercialProfileViewSet, basename="vendor-commercial-profile")
router.register("client-allocations", VendorClientAllocationViewSet, basename="vendor-client-allocation")
router.register("survey-allocations", VendorSurveyAllocationViewSet, basename="vendor-survey-allocation")
router.register("reservations", AllocationReservationViewSet, basename="allocation-reservation")

urlpatterns = router.urls
urlpatterns += [path("management-options/", VendorManagementOptionsView.as_view(), name="vendor-management-options")]
