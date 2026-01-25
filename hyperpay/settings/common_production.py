"""Common Settings"""
from typing import Any


def plugin_settings(settings: Any) -> None:
    """
    plugin settings
    """
    settings.HYPERPAY_SETTINGS = getattr(
        settings,
        'HYPERPAY_SETTINGS',
        {},
    )

    settings.HYPERPAY_MADA_SETTINGS = getattr(
        settings,
        'HYPERPAY_MADA_SETTINGS',
        {},
    )
