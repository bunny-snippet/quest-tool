from django.urls import path

from .views import vendor_management_page


urlpatterns = [
    path("vendors/", vendor_management_page, name="vendor-management"),
]
