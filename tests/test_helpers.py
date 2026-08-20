"""Test helpers method."""
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem

from hyperpay.exceptions import HyperPayException
from hyperpay.helpers import MANDATORY_FIELDS, format_gateway_amount, verify_success_response_with_cart


@pytest.fixture
def cart():
    """create cart."""
    user = get_user_model().objects.create(username='test-user', email='test@example.com')
    item = CatalogueItem.objects.create(sku='abcd', type='paid_course', price=100, currency='SAR')
    cart_obj = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    CartItem.objects.create(
        catalogue_item=item,
        original_price=item.price,
        final_price=item.price,
        cart=cart_obj,
    )
    return cart_obj


@pytest.mark.django_db
def test_successful_response(cart):  # pylint: disable=redefined-outer-name
    response = {
        field: 'value' for field in MANDATORY_FIELDS
    }
    response.update({
        'amount': '100.00',
        'currency': settings.ZEITLABS_PAYMENTS_SETTINGS['valid_currency'],
        'result': {'code': '000.100.110'},
        'card': {
            'bin': '411111',
            'last4Digits': '1111',
            'holder': 'JohnDoe',
            'expiryMonth': '12',
            'expiryYear': '2030'
        },
        'cart': {'items': [1]},
    })
    verify_success_response_with_cart(response, cart)


@pytest.mark.django_db
@pytest.mark.parametrize('missing_field', MANDATORY_FIELDS)
def test_missing_mandatory_field(cart, missing_field):  # pylint: disable=redefined-outer-name
    response = {field: 'value' for field in MANDATORY_FIELDS if field != missing_field}
    with pytest.raises(HyperPayException, match=f'Missing field in response: {missing_field}'):
        verify_success_response_with_cart(response, cart)


@pytest.mark.django_db
def test_amount_mismatch(cart):  # pylint: disable=redefined-outer-name
    response = {
        field: 'value' for field in MANDATORY_FIELDS
    }
    response.update({
        'amount': '200.00',
        'currency': settings.ZEITLABS_PAYMENTS_SETTINGS['valid_currency'],
        'result': {'code': '000.100.110'},
        'cart': {'items': [1]},
    })
    with pytest.raises(HyperPayException) as exc:
        verify_success_response_with_cart(response, cart)
    assert str(exc.value) == 'Transmitted amount (100.00) does not match response amount (200.00)'


@pytest.mark.django_db
def test_amount_three_decimal_fraction_raises_refusal(cart):  # pylint: disable=redefined-outer-name
    """Verify that a total that cannot be expressed in two decimals is refused, not rounded."""
    cart.items.all().update(final_price=Decimal('100.005'))
    with pytest.raises(HyperPayException, match='cannot be expressed in 0.01 precision'):
        format_gateway_amount(cart.total)


@pytest.mark.django_db
def test_invalid_currency(cart):  # pylint: disable=redefined-outer-name
    response = {
        field: 'value' for field in MANDATORY_FIELDS
    }
    response.update({
        'amount': '100.00',
        'currency': 'INVALID',
        'result': {'code': '000.100.110'},
        'cart': {'items': [1]},
    })
    with pytest.raises(HyperPayException, match='Invalid currency'):
        verify_success_response_with_cart(response, cart)


@pytest.mark.django_db
def test_missing_result_code(cart):  # pylint: disable=redefined-outer-name
    response = {
        field: 'value' for field in MANDATORY_FIELDS
    }
    response.update({
        'amount': '100.00',
        'currency': settings.ZEITLABS_PAYMENTS_SETTINGS['valid_currency'],
        'result': {},  # missing code
        'cart': {'items': [1]},
    })
    with pytest.raises(HyperPayException, match='Missing or invalid result.code'):
        verify_success_response_with_cart(response, cart)


@pytest.mark.django_db
def test_missing_card_field(cart):  # pylint: disable=redefined-outer-name
    response = {
        field: 'value' for field in MANDATORY_FIELDS
    }
    response.update({
        'amount': '100.00',
        'currency': settings.ZEITLABS_PAYMENTS_SETTINGS['valid_currency'],
        'result': {'code': '000.100.110'},
        'card': {'bin': '411111'},  # missing required fields
        'cart': {'items': [1]},
    })
    with pytest.raises(HyperPayException, match='Missing card field'):
        verify_success_response_with_cart(response, cart)


@pytest.mark.django_db
def test_cart_items_count_mismatch(cart):  # pylint: disable=redefined-outer-name
    response = {
        field: 'value' for field in MANDATORY_FIELDS
    }
    response.update({
        'amount': '100.00',
        'currency': settings.ZEITLABS_PAYMENTS_SETTINGS['valid_currency'],
        'result': {'code': '000.100.110'},
        'cart': {'items': [1, 2]},  # only 2 items, cart has 1
    })
    with pytest.raises(HyperPayException, match='Mismatch in number of cart items'):
        verify_success_response_with_cart(response, cart)
