"""Helpers for Hyperpay."""

from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from zeitlabs_payments.helpers import get_settings
from zeitlabs_payments.models import Cart

from hyperpay.exceptions import HyperPayException

MANDATORY_FIELDS = [
    'id', 'paymentType', 'paymentBrand', 'amount', 'currency',
    'merchantTransactionId', 'result'
]


def verify_success_response_with_cart(response: Dict[str, Any], cart: Cart) -> None:
    """
    Verify the format of a HyperPay response.

    :param response: The HyperPay response data.
    :raises HyperPayException: If validation fails.
    """
    for field in MANDATORY_FIELDS:
        if field not in response:
            raise HyperPayException(f"Missing field in response: {field}")

    amount = response['amount']
    try:
        amount_decimal = Decimal(amount)
        if cart.total != amount_decimal:
            raise HyperPayException(
                f'Cart total ({cart.total}) does not match response amount ({amount_decimal})'
            )
    except (InvalidOperation, Exception) as exc:
        raise HyperPayException(
            f'Error comparing cart total in response with cart total: {cart.total}. Amount received: {amount}'
        ) from exc

    if response['currency'] != get_settings().valid_currency:
        raise HyperPayException(f"Invalid currency: {response['currency']}")

    result = response.get('result', {})
    code = result.get('code')
    if not code or not isinstance(code, str):
        raise HyperPayException("Missing or invalid result.code")

    card = response.get('card', {})
    if card:
        required_card_fields = ['bin', 'last4Digits', 'holder', 'expiryMonth', 'expiryYear']
        for field in required_card_fields:
            if field not in card:
                raise HyperPayException(f"Missing card field: {field}")

    response_items = response.get('cart', {}).get('items', [])
    if len(response_items) != cart.items.count():
        raise HyperPayException(
            f"Mismatch in number of cart items: local={cart.items.count()}, response={len(response_items)}"
        )
