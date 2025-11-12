"""Hyperpay processor tests."""
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest
from django.test import TestCase
from zeitlabs_payments.models import Cart, CatalogueItem

from hyperpay.processor import HyperPay

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

    @patch('hyperpay.processor.configuration_helpers.get_value')
    @patch('hyperpay.processor.reverse')
    def test_init_sets_attributes(self, mock_reverse, mock_get_value):
        """Test Hyperpay __init__ properly sets attributes from settings and URL helpers."""
        mock_reverse.return_value = '/hyperpay/return/'
        mock_get_value.return_value = 'https://lms.example.com'
        processor = HyperPay()
        assert processor.client.client_id == 'fake-test-client'
        assert processor.client.client_secret == 'fake-test-secret'
        assert processor.client.base_url == 'https://test-fake-api.nelc.gov.sa'
        assert processor.client.slug == 'hyperpay'
        assert processor.payment_url == 'https://fake.com/v1/paymentWidgets.js'
        assert processor.return_url == 'https://lms.example.com/hyperpay/return/'

    def test_get_payment_method_metadata_returns_expected(self):
        """Test get_payment_method_metadata returns correct dict with slug, title, checkout_text, and URL."""
        result = HyperPay.get_payment_method_metadata(self.cart)
        assert result['slug'] == HyperPay.SLUG
        assert result['title'] == HyperPay.NAME
        assert 'checkout_text' in result
        assert 'url' in result
        assert str(self.cart.id) in result['url']

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
        assert payload["payment_method"] == processor.BRAND
        assert payload["amount"] == str(int(round(self.cart.total * 100, 0)))
        assert "merchant_transaction_id" in payload
        assert payload["customer_email"] == self.user.email

        assert result["checkout_id"] == "chk_123"
        assert result["return_url"] == processor.return_url
        assert result["payment_page_url"].startswith(processor.payment_url)
        assert result["csrfmiddlewaretoken"] == "csrf123"

    @patch("hyperpay.processor.render")
    @patch.object(HyperPay, "get_transaction_parameters")
    def test_payment_view_renders_template_with_correct_context(
        self, mock_get_transaction_parameters, mock_render
    ):
        """Test payment_view calls get_transaction_parameters and renders with correct context."""
        mock_get_transaction_parameters.return_value = {
            "checkout_id": "chk_123",
            "payment_page_url": "https://fake.com/widget?checkoutId=chk_123",
            "return_url": "https://example.com/return",
        }
        processor = HyperPay()
        processor.payment_view(cart=self.cart, request=self.fake_request)
        mock_get_transaction_parameters.assert_called_once_with(
            cart=self.cart,
            request=self.fake_request,
            use_client_side_checkout=False,
        )
        mock_render.assert_called_once_with(
            self.fake_request,
            "hyperpay/hyperpay.html",
            {"transaction_parameters": mock_get_transaction_parameters.return_value},
        )
