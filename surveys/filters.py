import django_filters

from .models import Survey, SurveyAttempt


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """Accept comma-separated values, e.g. ?country=US,IN."""


class SurveyFilter(django_filters.FilterSet):
    client = django_filters.NumberFilter(field_name="client_id", help_text="Internal client record ID")
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
        fields = ["client", "country", "language", "status", "company", "created_from", "created_to", "modified_from", "modified_to", "min_cpi", "max_cpi"]


class SurveyAttemptFilter(django_filters.FilterSet):
    status = CharInFilter(field_name="status", lookup_expr="in", help_text="Comma-separated attempt statuses")
    user = CharInFilter(field_name="platform_user_id", lookup_expr="in", help_text="Comma-separated platform user IDs")
    company = CharInFilter(field_name="survey__company_name", lookup_expr="in")
    survey_id = django_filters.NumberFilter(field_name="survey__source_id")
    internal_id = django_filters.CharFilter(field_name="survey__local_id", lookup_expr="iexact")
    initiated_from = django_filters.IsoDateTimeFilter(field_name="initiated_at", lookup_expr="gte")
    initiated_to = django_filters.IsoDateTimeFilter(field_name="initiated_at", lookup_expr="lte")
    callback_from = django_filters.IsoDateTimeFilter(field_name="callback_at", lookup_expr="gte")
    callback_to = django_filters.IsoDateTimeFilter(field_name="callback_at", lookup_expr="lte")
    entry_ip = django_filters.CharFilter(field_name="initiation_ip", lookup_expr="iexact")
    exit_ip = django_filters.CharFilter(field_name="callback_ip", lookup_expr="iexact")

    class Meta:
        model = SurveyAttempt
        fields = [
            "status", "user", "company", "survey_id", "internal_id", "initiated_from", "initiated_to",
            "callback_from", "callback_to", "entry_ip", "exit_ip",
        ]
