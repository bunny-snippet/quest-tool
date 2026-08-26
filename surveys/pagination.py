"""Shared bounded page-number pagination for workspace REST lists."""

from django.core.paginator import InvalidPage
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination

from .project_cache import project_filtered_count


class SurveyPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        """Use Redis only for Projects counts; page rows remain authoritative."""

        self.request = request
        page_size = self.get_page_size(request)
        if not page_size:
            return None

        paginator = self.django_paginator_class(queryset, page_size)
        if getattr(view, "project_count_cache_enabled", False):
            # Django's count is a cached_property. Seeding only this value saves
            # an expensive COUNT query without caching any user-specific row,
            # CPI, permission decision, or respondent start link.
            paginator.__dict__["count"] = project_filtered_count(request, queryset)
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
