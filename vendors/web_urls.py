from django.urls import path

from .views import organization_management_page, vendor_management_page


urlpatterns = [
    path("vendors/", vendor_management_page, name="vendor-management"),
    path("organization/", organization_management_page, name="organization-management"),
]
