"""Low-overhead DRF filter backends with unchanged filtering semantics."""

from django_filters import utils
from django_filters.rest_framework import DjangoFilterBackend


class SparseDjangoFilterBackend(DjangoFilterBackend):
    """Skip FilterSet/form construction when no declared filter is supplied.

    ``django-filter`` deep-copies every declared filter while instantiating a
    FilterSet. That validation work is required when a filter parameter is
    present, but it cannot affect an unfiltered request. Unknown query
    parameters remain ignored exactly as they are by DjangoFilterBackend.
    """

    def filter_queryset(self, request, queryset, view):
        filterset_class = self.get_filterset_class(view, queryset)
        if filterset_class is None:
            return queryset
        if not any(name in request.query_params for name in filterset_class.base_filters):
            return queryset

        kwargs = self.get_filterset_kwargs(request, queryset, view)
        filterset = filterset_class(**kwargs)
        if not filterset.is_valid() and self.raise_exception:
            raise utils.translate_validation(filterset.errors)
        return filterset.qs
