"""Hyperpay processor."""

import logging
from typing import Any, Optional
from urllib.parse import urljoin

from django.conf import settings
from django.http import HttpRequest
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from zeitlabs_payments.helpers import get_settings as zeitlabs_payments_settings
from zeitlabs_payments.models import Cart
from zeitlabs_payments.providers.base import BaseProcessor

from hyperpay.client import HyperPayClient

logger = logging.getLogger(__name__)


class HyperPay(BaseProcessor):
    """
    HyperPay processor (business logic + Django integration).
    """
    SLUG = 'hyperpay'
    CHECKOUT_TEXT = _('Checkout with HyperPay Credit Card')
    NAME = 'HyperPay'
    BRANDS = 'VISA MASTER'

    TEMPLATE_NAME = 'hyperpay/hyperpay.html'

    def __init__(self) -> None:
        """Initialize the HyperPay processor with client + config."""
        self.client = HyperPayClient(
            access_token=self.processor_settings['access_token'],
            base_url=self.processor_settings['base_url'],
            entity_id=self.processor_settings['entity_id'],
            test_mode=self.processor_settings.get('test_mode'),
            slug=self.SLUG
        )
        self.payment_url = f"{settings.HYPERPAY_SETTINGS['API_URL']}/v1/paymentWidgets.js"
        self.return_url = urljoin(zeitlabs_payments_settings().root_url, reverse("hyperpay:return"))

    def get_processor_settings(self) -> dict:  # pylint: disable=self-use-argument
        """Return processor settings."""
        return {
            'access_token': settings.HYPERPAY_SETTINGS['ACCESS_TOKEN'],
            'base_url': settings.HYPERPAY_SETTINGS['API_URL'],
            'entity_id': settings.HYPERPAY_SETTINGS['ENTITY_ID'],
            'test_mode': settings.HYPERPAY_SETTINGS.get('TEST_MODE'),
        }

    @property
    def processor_settings(self) -> dict:
        """Return processor settings property."""
        return self.get_processor_settings()

    def get_cart_data(self, cart: Cart) -> dict:
        """Return cart items details."""
        data = {}
        index = 0
        for item in cart.items.all():
            data.update({
                f'cart.items[{index}].name': item.catalogue_item.title,
                f'cart.items[{index}].description': item.catalogue_item.description,
                f'cart.items[{index}].currency': item.catalogue_item.currency,
                f'cart.items[{index}].sku': item.catalogue_item.sku,
                f'cart.items[{index}].originalPrice': item.original_price,
                f'cart.items[{index}].taxAmount': item.tax_amount,
            })
            index += 1
        return data

    def get_transaction_parameters(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any,
    ) -> dict:
        """
        Build the required parameters for initiating a payment.
        """
        base_params = super().get_transaction_parameters_base(cart, request)
        checkout_payload = {
            'customer.email': base_params['user_email'],
            'amount': f"{base_params['amount']:.2f}",
            'currency': str(base_params['currency']),
            'merchantTransactionId': base_params['order_reference'].zfill(8)
        }
        checkout_payload.update(self.get_cart_data(cart))
        transaction_parameters = self.client.create_checkout(checkout_payload)
        checkout_id = transaction_parameters['checkout_id']

        transaction_parameters.update(
            {
                'return_url': self.return_url,
                'payment_page_url': f'{self.payment_url}?checkoutId={checkout_id}',
                'csrfmiddlewaretoken': get_token(request),
                'brands': self.BRANDS,
                'locale': request.LANGUAGE_CODE if request else 'en',
            }
        )
        return transaction_parameters


class HyperPayMada(HyperPay):
    """HyperPay Mada processor."""
    SLUG = 'hyperpay_mada'
    CHECKOUT_TEXT = _('Checkout with HyperPay Mada')
    NAME = 'HyperPay Mada'
    BRANDS = 'MADA'

    def get_processor_settings(self) -> dict:  # pylint: disable=self-use-argument
        """Return processor settings."""
        return {
            'access_token': settings.HYPERPAY_MADA_SETTINGS['ACCESS_TOKEN'],
            'base_url': settings.HYPERPAY_MADA_SETTINGS['API_URL'],
            'entity_id': settings.HYPERPAY_MADA_SETTINGS['ENTITY_ID'],
            'test_mode': settings.HYPERPAY_MADA_SETTINGS.get('TEST_MODE'),
        }
