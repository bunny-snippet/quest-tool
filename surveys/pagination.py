from django.core.paginator import InvalidPage
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.pagination import PageNumberPagination

from accounts.access import has_function_access
from .project_cache import project_filtered_count

class SurveyPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_page_size(self, request):
        requested = request.query_params.get(self.page_size_query_param)

        if requested not in {None, ""}:
            view = getattr(self, "_active_view", None)

            if (
                getattr(view, "project_count_cache_enabled", False)
                and not has_function_access(
                    request.user,
                    "projects.control.page_size",
                )
            ):
                return self.page_size

        return super().get_page_size(request)

    def paginate_queryset(self, queryset, request, view=None):
        self.request = request
        self._active_view = view

        # Projects pagination permission
        if (
            getattr(view, "project_count_cache_enabled", False)
            and request.query_params.get("page") not in {None, "", "1"}
            and not has_function_access(
                request.user,
                "projects.control.pagination",
            )
        ):
            raise PermissionDenied(
                "Your account cannot navigate project pages."
            )

        page_size = self.get_page_size(request)
        if not page_size:
            return None

        paginator = self.django_paginator_class(queryset, page_size)

        if getattr(view, "project_count_cache_enabled", False):
            paginator.__dict__["count"] = project_filtered_count(
                request,
                queryset,
            )

        page_number = self.get_page_number(request, paginator)

        try:
            self.page = paginator.page(page_number)
        except InvalidPage as exc:
            message = self.invalid_page_message.format(
                page_number=page_number,
                message=str(exc),
            )
            raise NotFound(message) from exc

        if paginator.num_pages > 1 and self.template is not None:
            self.display_page_controls = True

        return list(self.page)