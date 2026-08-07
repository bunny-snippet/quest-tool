import django_filters

from .models import Survey


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """Accept comma-separated values, e.g. ?country=US,IN."""


class SurveyFilter(django_filters.FilterSet):
    country = CharInFilter(field_name="country_code", lookup_expr="in", help_text="Comma-separated country codes, e.g. US,IN")
    language = CharInFilter(field_name="language_code", lookup_expr="in", help_text="Comma-separated language codes, e.g. EN,HI")
    status = CharInFilter(field_name="status", lookup_expr="in", help_text="Comma-separated statuses: live,closed")
    company = CharInFilter(field_name="company_name", lookup_expr="in", help_text="Comma-separated supplier company names")
    created_from = django_filters.IsoDateTimeFilter(field_name="source_created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="source_created_at", lookup_expr="lte")
    modified_from = django_filters.IsoDateTimeFilter(field_name="source_modified_at", lookup_expr="gte")
    modified_to = django_filters.IsoDateTimeFilter(field_name="source_modified_at", lookup_expr="lte")
    min_cpi = django_filters.NumberFilter(field_name="cpi", lookup_expr="gte")
    max_cpi = django_filters.NumberFilter(field_name="cpi", lookup_expr="lte")

    class Meta:
        model = Survey
        fields = ["country", "language", "status", "company", "created_from", "created_to", "modified_from", "modified_to", "min_cpi", "max_cpi"]
