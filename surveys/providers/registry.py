"""Provider adapter registration and client-integration catalog metadata."""

from .base import ProviderConfigurationError


def _provider_classes():
    """Import specialized adapters lazily to avoid provider/model import cycles."""

    from .cint import CintProvider
    from .purespectrum import PureSpectrumProvider
    from .rfg import ResearchForGoodProvider

    return {
        ResearchForGoodProvider.code: ResearchForGoodProvider,
        CintProvider.code: CintProvider,
        PureSpectrumProvider.code: PureSpectrumProvider,
    }


def has_provider(code: str) -> bool:
    """Return whether ``code`` has a specialized runtime adapter."""

    return str(code or "").lower() in _provider_classes()


def provider_catalog() -> list[dict]:
    """Return provider choices/defaults rendered by client-integration forms."""

    installed = [
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
    generic = [
        {
            "code": "innovatemr",
            "label": "InnovateMR",
            "default_base_url": "https://supplier.innovatemr.net/api/v2",
            "minimum_sync_interval_seconds": 60,
            "credential_fields": [{"key": "token", "label": "API token"}],
        },
        {
            "code": "biobrain",
            "label": "BioBrain / Voqall",
            "default_base_url": "https://partner-api.voqall.com/api/v1/surveys",
            "minimum_sync_interval_seconds": 60,
            "credential_fields": [{"key": "token", "label": "Partner access key"}],
        },
        {
            "code": "custom",
            "label": "Custom REST API",
            "default_base_url": "",
            "minimum_sync_interval_seconds": 60,
            "credential_fields": [{"key": "token", "label": "API token"}],
        },
    ]
    return installed + generic


def get_provider(integration, *, session=None):
    """Instantiate the adapter selected by a persisted client integration."""

    provider_class = _provider_classes().get(integration.provider_code)
    if not provider_class:
        raise ProviderConfigurationError(
            f"Provider adapter '{integration.provider_code}' is not installed."
        )
    return provider_class(integration, session=session)
