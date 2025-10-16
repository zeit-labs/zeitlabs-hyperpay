"""Hyperpay processor."""

import logging
from typing import Any, Optional
from urllib.parse import urljoin

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from zeitlabs_payments import configuration_helpers
from zeitlabs_payments.models import Cart
from zeitlabs_payments.providers.base import BaseProcessor

from hyperpay.client import HyperPayClient

logger = logging.getLogger(__name__)


class HyperPay(BaseProcessor):
    """
    HyperPay processor (business logic + Django integration).
    """
    SLUG = "hyperpay"
    CHECKOUT_TEXT = _("Checkout with HyperPay credit card")
    NAME = "HyperPay"
    BRAND = "VISA/MasterCard"

    TRANSACTION_STATUS_PENDING = "pending"
    TRANSACTION_STATUS_SUCCESS = "success"

    def __init__(self) -> None:
        """Initialize the HyperPay processor with client + config."""
        self.client = HyperPayClient(
            client_id=settings.HYPERPAY_SETTINGS["CLIENT_ID"],
            client_secret=settings.HYPERPAY_SETTINGS["CLIENT_SECRET"],
            base_url=settings.HYPERPAY_SETTINGS["NELC_API_URL"],
            slug=self.SLUG
        )
        self.payment_url = settings.HYPERPAY_SETTINGS['PAYMENT_WIDGET_URL']
        self.return_url = urljoin(
            configuration_helpers.get_value("LMS_ROOT_URL", settings.ECOMMERCE_PUBLIC_URL_ROOT),
            reverse("hyperpay:return"),
        )

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
            "customer_email": base_params["user_email"],
            "payment_method": self.BRAND,
            "amount": str(base_params["amount"]),
            "merchant_transaction_id": base_params['order_reference'].zfill(8)
        }
        transaction_parameters = self.client.create_checkout(checkout_payload)
        checkout_id = transaction_parameters["checkout_id"]

        transaction_parameters.update(
            {
                "return_url": self.return_url,
                "payment_page_url": f"{self.payment_url}?checkoutId={checkout_id}",
                "csrfmiddlewaretoken": get_token(request),
            }
        )
        return transaction_parameters

    def payment_view(
        self,
        cart: Cart,
        request: Optional[HttpRequest] = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        Render the payment redirection view.
        """
        transaction_parameters = self.get_transaction_parameters(
            cart=cart,
            request=request,
            use_client_side_checkout=use_client_side_checkout,
            **kwargs,
        )
        return render(
            request,
            f"hyperpay/{self.SLUG}.html",
            {"transaction_parameters": transaction_parameters},
        )
