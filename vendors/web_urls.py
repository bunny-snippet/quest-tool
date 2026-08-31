"""HTML routes for Suppliers, Client APIs and Organization workspaces."""

from django.urls import path

from .views import (
    client_integrations_page,
    organization_management_page,
    supplier_api_guide,
    vendor_management_page,
)


urlpatterns = [
    path("supplier-api-guide/", supplier_api_guide, name="supplier-api-guide"),
    path("vendors/", vendor_management_page, name="vendor-management"),
    path("client-integrations/", client_integrations_page, name="client-integrations"),
    path("organization/", organization_management_page, name="organization-management"),
]
