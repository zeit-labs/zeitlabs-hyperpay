"""Helpers for Hyperpay."""

from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from zeitlabs_payments.helpers import get_settings as zeitlabs_payments_settings
from zeitlabs_payments.models import Cart

from hyperpay.exceptions import HyperPayException

MANDATORY_FIELDS = [
    'id', 'paymentType', 'paymentBrand', 'amount', 'currency',
    'merchantTransactionId', 'result'
]

#: The gateway price fields accept at most two decimal places.
GATEWAY_AMOUNT_PRECISION = Decimal('0.01')


def format_gateway_amount(amount: Decimal) -> str:
    """
    Format a monetary amount for the gateway.

    The amount is returned with exactly two decimal places. Conversion is exact: an amount
    that cannot be expressed in two decimal places raises instead of rounding, so the value
    charged is never silently changed.
    """
    quantized = amount.quantize(GATEWAY_AMOUNT_PRECISION)
    if quantized != amount:
        raise HyperPayException(
            f'Amount {amount} cannot be expressed in {GATEWAY_AMOUNT_PRECISION} precision without rounding.'
        )
    return str(quantized)


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
    except InvalidOperation as exc:
        raise HyperPayException(
            f'Error comparing amount with the transmitted amount: {format_gateway_amount(cart.total)}. '
            f'Amount received: {amount}'
        ) from exc

    transmitted_amount = Decimal(format_gateway_amount(cart.total))
    if transmitted_amount != amount_decimal:
        raise HyperPayException(
            f'Transmitted amount ({transmitted_amount}) does not match response amount ({amount_decimal})'
        )

    if response['currency'] != zeitlabs_payments_settings().valid_currency:
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
