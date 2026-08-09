from .base import ProviderConfigurationError


def _provider_classes():
    from .rfg import ResearchForGoodProvider

    return {ResearchForGoodProvider.code: ResearchForGoodProvider}


def provider_catalog() -> list[dict]:
    return [
        {
            "code": provider.code,
            "label": provider.label,
            "default_base_url": provider.default_base_url,
            "minimum_sync_interval_seconds": provider.minimum_sync_interval_seconds,
            "credential_fields": [
                {"key": key, "label": label} for key, label in provider.credential_fields
            ],
        }
        for provider in _provider_classes().values()
    ]


def get_provider(integration, *, session=None):
    provider_class = _provider_classes().get(integration.provider_code)
    if not provider_class:
        raise ProviderConfigurationError(
            f"Provider adapter '{integration.provider_code}' is not installed."
        )
    return provider_class(integration, session=session)
