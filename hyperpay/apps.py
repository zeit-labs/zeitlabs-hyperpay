"""
hyperpay Django application initialization.
"""

from django.apps import AppConfig


class HyperpayConfig(AppConfig):
    """
    Configuration for the hyperpay Django application.
    """

    name = 'hyperpay'

    plugin_app = {
        'settings_config': {
            'lms.djangoapp': {
                'production': {
                    'relative_path': 'settings.common_production',
                }
            }
        },
        'url_config': {
            'lms.djangoapp': {
                'namespace': 'hyperpay',
                'regex': '^hyperpay/',
                'relative_path': 'urls',
            },
        },
    }
