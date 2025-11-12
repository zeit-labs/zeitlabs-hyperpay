"""
These settings are here to use during tests, because django requires them.

In a real-world use case, apps in this project are installed into other
Django applications, so these settings will not be used.
"""

from os.path import abspath, dirname, join


def root(*args):
    """
    Get the absolute path of the given path relative to the project root.
    """
    return join(abspath(dirname(__file__)), *args)


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': 'default.db',
        'USER': '',
        'PASSWORD': '',
        'HOST': '',
        'PORT': '',
    }
}

INSTALLED_APPS = (
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.messages',
    'django.contrib.sessions',
    'django.contrib.sites',
    'zeitlabs_payments',
    'hyperpay',
)

LOCALE_PATHS = [
    root('hyperpay', 'conf', 'locale'),
]

ROOT_URLCONF = 'tests.test_urls'

SECRET_KEY = 'insecure-secret-key'

MIDDLEWARE = (
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
)

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': ['tests/templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.contrib.auth.context_processors.auth',  # this is required for admin
            'django.contrib.messages.context_processors.messages',  # this is required for admin
            'django.template.context_processors.request'
        ],
    },
}]

# Avoid warnings about migrations
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

SITE_ID = 1

HYPERPAY_SETTINGS = {
    'CLIENT_ID': 'fake-test-client',
    'CLIENT_SECRET': 'fake-test-secret',
    'NELC_API_URL': 'https://test-fake-api.nelc.gov.sa',
    'PAYMENT_WIDGET_URL': 'https://fake.com/v1/paymentWidgets.js',
}
INVOICE_PREFIX = 'DEV'
VALID_CURRENCY = 'SAR'
ECOMMERCE_PUBLIC_URL_ROOT = 'test.ecommerce.com'
