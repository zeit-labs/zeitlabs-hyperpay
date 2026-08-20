"""Hyperpay processor tests."""
import io
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest
from django.test import TestCase
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem

from hyperpay.exceptions import HyperPayException
from hyperpay.processor import HyperPay, HyperPayMada, empty_hyperpay_settings

User = get_user_model()


@pytest.mark.django_db
class TestHyperPayProcessor(TestCase):
    """HyperpayProcessor Tests"""

    def setUp(self) -> None:
        """
        Set up test data for the hyperpay webhook callback.
        """
        self.user = User.objects.create(username='test-user', email='test@example.com')
        self.cart = Cart.objects.create(user=self.user, status=Cart.Status.PROCESSING)
        self.course_item = CatalogueItem.objects.create(
            sku='sku-i',
            type=CatalogueItem.ItemType.PAID_COURSE,
            item_ref_id='course-v1:test+1+1',
            price='1000',
            currency='SAR'
        )
        self.cart.items.create(
            catalogue_item=self.course_item,
            original_price=self.course_item.price,
            final_price=self.course_item.price,
        )
        self.fake_request = MagicMock(spec=HttpRequest)
        self.fake_request.build_absolute_uri.return_value = 'https://example.com'
        self.fake_request.site = Site.objects.get(domain='example.com')
        self.fake_request.LANGUAGE_CODE = 'en'

    @patch('zeitlabs_payments.providers.base.get_plugin_settings')
    @patch('hyperpay.processor.zeitlabs_payments_settings')
    @patch('hyperpay.processor.reverse')
    def test_init_sets_attributes(self, mock_reverse, mock_get_value, mock_base_settings):
        """Test Hyperpay __init__ properly sets attributes from settings and URL helpers."""
        mock_reverse.return_value = '/hyperpay/return/'
        mock_settings_instance = MagicMock(root_url='https://lms.example.com')
        mock_get_value.return_value = mock_settings_instance
        mock_base_settings.return_value.get_by_root_key.return_value = settings.HYPERPAY_SETTINGS
        processor = HyperPay()
        assert processor.client.base_url == 'https://test-fake-api.nelc.gov.sa'
        assert processor.client.slug == 'hyperpay'
        assert processor.client.access_token == 'fake-test'
        assert processor.client.entity_id == '12345'
        assert processor.client.test_mode == 'EXTERNAL'
        assert processor.payment_url == 'https://test-fake-api.nelc.gov.sa/v1/paymentWidgets.js'
        assert processor.return_url == 'https://lms.example.com/hyperpay/return/'

    def test_get_payment_method_metadata_returns_expected(self):
        """Test get_payment_method_metadata returns correct dict with slug, title, checkout_text, and URL."""
        result = HyperPay.get_payment_method_metadata(self.cart)
        assert result['slug'] == HyperPay.SLUG
        assert result['title'] == HyperPay.NAME
        assert 'checkout_text' in result
        assert 'url' in result
        assert str(self.cart.id) in result['url']

    def test_is_hidden_for_returns_false_when_configured(self):
        """Verify that HyperPay is offered when it has an access_token for the site."""
        request = MagicMock(spec=HttpRequest)
        assert HyperPay().is_hidden_for(request) is False

    @patch('zeitlabs_payments.providers.base.get_plugin_settings')
    @patch('hyperpay.processor.zeitlabs_payments_settings')
    def test_is_hidden_for_returns_true_when_unconfigured(self, mock_get_value, mock_base_settings):
        """Verify that HyperPay hides itself when no settings are available for the site."""
        mock_settings_instance = MagicMock(root_url='https://lms.example.com')
        mock_get_value.return_value = mock_settings_instance
        mock_base_settings.return_value.get_by_root_key.return_value = empty_hyperpay_settings
        request = MagicMock(spec=HttpRequest)
        assert HyperPay().is_hidden_for(request) is True

    @patch("hyperpay.processor.get_token", return_value="csrf123")
    @patch("hyperpay.processor.HyperPayClient")
    @patch('zeitlabs_payments.helpers.get_course_id')
    def test_get_transaction_parameters_builds_correct_payload(
        self,
        mock_get_course_id,
        mock_client_class,
        mock_get_token,  # pylint: disable=unused-argument
    ):
        """Test get_transaction_parameters builds correct payload and merges client response."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_checkout.return_value = {
            "checkout_id": "chk_123",
            "result_code": "000.100.110",
            "result_description": "Request successfully processed"
        }
        mock_client_class.return_value = mock_client_instance
        mock_get_course_id.return_value = 'course-v1:test+1+1'
        processor = HyperPay()
        result = processor.get_transaction_parameters(cart=self.cart, request=self.fake_request)

        mock_client_instance.create_checkout.assert_called_once()
        payload = mock_client_instance.create_checkout.call_args[0][0]
        assert payload["amount"] == '1000.00', 'amount is the exact two-decimal gateway format'
        assert "merchantTransactionId" in payload
        assert payload["customer.email"] == self.user.email
        assert payload["cart.items[0].originalPrice"] == '1000.00', \
            'originalPrice must be formatted to two decimals for the gateway regex'
        assert payload["cart.items[0].taxAmount"] == '0.00', \
            'taxAmount must be formatted to two decimals for the gateway regex'

        assert result["checkout_id"] == "chk_123"
        assert result["return_url"] == processor.return_url
        assert result["payment_page_url"].startswith(processor.payment_url)
        assert result["locale"] == "en"
        assert result["csrfmiddlewaretoken"] == "csrf123"

    @patch("hyperpay.processor.get_token", return_value="csrf123")
    @patch("hyperpay.processor.HyperPayClient")
    @patch('zeitlabs_payments.helpers.get_course_id')
    def test_get_transaction_parameters_refuses_inexpressible_amount(
        self,
        mock_get_course_id,
        mock_client_class,
        mock_get_token,  # pylint: disable=unused-argument
    ):
        """Verify that a total that cannot be expressed in two decimals is refused at payload build."""
        self.cart.items.all().update(final_price=Decimal('100.005'))
        mock_client_class.return_value = MagicMock()
        mock_get_course_id.return_value = 'course-v1:test+1+1'
        processor = HyperPay()
        with pytest.raises(HyperPayException, match='cannot be expressed in 0.01 precision'):
            processor.get_transaction_parameters(cart=self.cart, request=self.fake_request)

    def test_payment_template_has_no_inert_meta_csp(self):
        """Verify that the payment page no longer emits the inert meta Content-Security-Policy."""
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'hyperpay', 'templates', 'hyperpay', 'hyperpay.html',
        )
        with io.open(template_path, encoding='utf8') as template_file:
            content = template_file.read()
        assert 'Content-Security-Policy' not in content

    @patch("hyperpay.processor.get_token", return_value="csrf123")
    @patch("hyperpay.processor.HyperPayClient")
    @patch('zeitlabs_payments.helpers.get_course_id')
    def test_get_transaction_parameters_honours_arabic_language(
        self,
        mock_get_course_id,
        mock_client_class,
        mock_get_token,  # pylint: disable=unused-argument
    ):
        """Verify that an Arabic request reaches the gateway widget with locale 'ar'."""
        mock_client_instance = MagicMock()
        mock_client_instance.create_checkout.return_value = {
            "checkout_id": "chk_456",
            "result_code": "000.100.110",
            "result_description": "Request successfully processed"
        }
        mock_client_class.return_value = mock_client_instance
        mock_get_course_id.return_value = 'course-v1:test+1+1'
        self.fake_request.LANGUAGE_CODE = 'ar'
        processor = HyperPay()
        result = processor.get_transaction_parameters(cart=self.cart, request=self.fake_request)
        assert result["locale"] == "ar"

    def test_get_cart_from_reference_success(self):
        reference = f'0011-{self.cart.id}'
        processor = HyperPay()
        actual_cart = processor.get_cart_from_reference(reference)
        assert isinstance(actual_cart, Cart)
        assert actual_cart.id == self.cart.id

    def test_get_cart_from_reference_invalid(self):
        processor = HyperPay()
        assert not AuditLog.objects.filter(
            gateway='hyperpay', action=AuditLog.AuditActions.RESPONSE_INVALID_CART).exists()
        result = processor.get_cart_from_reference("invalid-reference")
        assert result is None
        assert AuditLog.objects.filter(
            gateway='hyperpay', action=AuditLog.AuditActions.RESPONSE_INVALID_CART
        ).exists()


def test_mada_processor_class():
    """verify that HyperPayMada is a subclass of HyperPay with correct attributes."""
    assert issubclass(HyperPayMada, HyperPay)
    assert HyperPayMada.SLUG == 'hyperpay_mada'
    assert HyperPayMada.NAME == 'HyperPay Mada'

    assert not any(value.startswith('mada-') for value in settings.HYPERPAY_SETTINGS.values())

    assert all((value.startswith('mada-') for value in settings.HYPERPAY_MADA_SETTINGS.values()))


def test_mada_processor_settings():
    """verify that HyperPayMada uses the correct settings."""
    assert not any(value.startswith('mada-') for value in settings.HYPERPAY_SETTINGS.values())
    assert all((value.startswith('mada-') for value in settings.HYPERPAY_MADA_SETTINGS.values()))

    mada_processor = HyperPayMada()
    assert all((value.startswith('mada-') for value in mada_processor.get_processor_settings().values()))
