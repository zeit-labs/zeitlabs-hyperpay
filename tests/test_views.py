
"""Test views for hyperpay provider"""
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from zeitlabs_payments.models import Cart, CartItem, CatalogueItem, Invoice, Transaction

from hyperpay.exceptions import HyperPayException
from hyperpay.processor import HyperPay

User = get_user_model()


class HyperPayReturnView(TestCase):
    """HyperpayReturnView Tests."""

    @patch('hyperpay.client.HyperPayClient.get_checkout_status')
    def test_get_success(self, processor_client_verify_status):
        checkout_id = '1234'
        processor_client_verify_status.return_value = {
            'result': {'code': '000.000.000'}, 'merchant_transaction_id': 'ABCD-00011-12', 'id': checkout_id
        }
        response = self.client.get(f"{reverse('hyperpay:return')}?id={checkout_id}")
        self.assertTemplateUsed(response, 'zeitlabs_payments/wait_feedback.html')
        assert response.context['ecommerce_transaction_id'] == '1234'
        assert response.context['ecommerce_status_url'] == reverse('hyperpay:status')
        assert response.context['ecommerce_error_url'] == reverse(
            'zeitlabs_payments:payment-error',
            args=[checkout_id]
        )
        assert response.context['ecommerce_success_url'] == reverse(
            'zeitlabs_payments:payment-success',
            args=[checkout_id]
        )
        assert response.context['ecommerce_max_attempts'] == 24
        assert response.context['ecommerce_wait_time'] == 5000

    def test_get_missing_checkout_id(self):
        """Should render payment_error.html when checkout_id is missing."""
        response = self.client.get(reverse('hyperpay:return'))
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')


@pytest.mark.django_db
class HyperPayStatusViewTest(TestCase):
    """Tests for HyperPayStatusView"""

    def setUp(self):
        """Setup"""
        self.payment_processor = HyperPay()
        self.user = User.objects.create(username='test-user')
        self.course_item = CatalogueItem.objects.create(
            sku='sku-i',
            type=CatalogueItem.ItemType.PAID_COURSE,
            item_ref_id='course-v1:test+1+1',
            price='100',
            currency='SAR'
        )
        self.fake_course_mode = Mock()
        self.fake_course_mode.course.id = self.course_item.item_ref_id

        self.processing_cart = Cart.objects.create(user=self.user, status=Cart.Status.PROCESSING)
        CartItem.objects.create(
            catalogue_item=self.course_item,
            original_price=self.course_item.price,
            final_price=self.course_item.price,
            cart=self.processing_cart,
        )
        self.unknown_cart = Cart.objects.create(user=self.user, status='UNKNOWN')
        self.paid_cart = Cart.objects.create(user=self.user, status=Cart.Status.PAID)
        self.payment_pending_cart = Cart.objects.create(user=self.user, status=Cart.Status.PAYMENT_PENDING)
        self.url = reverse('hyperpay:status')

        self.response_template = {
            'id': '11223344',
            'paymentBrand': 'VISA',
            'merchantTransactionId': f'0001-{self.processing_cart.id}',
            'amount': '100.00',
            'currency': 'SAR',
            'result': {'code': '000.100.110', 'description': 'successfully processed'},
            'card': {
                'bin': '411111',
                'last4Digits': '1111',
                'holder': 'JohnDoe',
                'expiryMonth': '12',
                'expiryYear': '2030'
            },
            'paymentType': 'debit',
            'cart': {'items': [1]},
        }

    def test_redirects_if_not_logged_in(self):
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        self.assertEqual(response.status_code, 302)

    def test_missing_merchant_identifier(self):
        self.client.force_login(self.user)
        response = self.client.get(f'{self.url}')
        assert response.status_code == 400
        assert response.json()['error'] == 'Merchant Reference is required to verify payment status.'

    @pytest.mark.django_db
    @patch("hyperpay.client.HyperPayClient.get_checkout_status")
    def test_get_success_for_checkout_status_exception(self, mock_client_checkout_status):
        self.client.force_login(self.user)
        mock_client_checkout_status.side_effect = HyperPayException('Some error - maybe API returned 400')
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    @pytest.mark.django_db
    @patch("hyperpay.client.requests.get")
    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    @patch("zeitlabs_payments.cart_handler.CourseMode")
    def test_get_success_with_success_payment(
        self, mock_course_mode, mock_enroll, mock_get  # pylint: disable=unused-argument
    ):
        self.client.force_login(self.user)
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = deepcopy(self.response_template)
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        mock_course_mode.objects.get.return_value = self.fake_course_mode

        assert not Invoice.objects.filter(cart=self.processing_cart).exists(), \
            'Invoice should not exist before test'
        assert not Transaction.objects.filter(gateway='hyperpay', cart=self.processing_cart).exists(), \
            'Transaction should not exist before test'
        assert self.processing_cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        response = self.client.get(f'{self.url}?merchant_reference=1122')
        assert response.status_code == 200

        invoice = Invoice.objects.get(cart=self.processing_cart)
        data = response.json()
        assert data['invoice'] == invoice.invoice_number
        assert data['invoice_url'] == reverse(
            'zeitlabs_payments:invoice',
            args=[invoice.invoice_number]
        )
        assert invoice.related_transaction.gateway_transaction_id == '11223344'

        self.processing_cart.refresh_from_db()
        assert self.processing_cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after successful payment'

    @pytest.mark.django_db
    @patch("hyperpay.client.requests.get")
    def test_get_success_with_invalid_response_total_amount_mismatched(self, mock_get):
        self.client.force_login(self.user)
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = deepcopy(self.response_template)
        mock_response.json.return_value['amount'] = 'invalid'
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        assert response.status_code == 200
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    @pytest.mark.django_db
    @patch("hyperpay.client.HyperPayClient.get_checkout_status")
    def test_get_success_for_failed_payment(self, mock_checkout_status):
        self.client.force_login(self.user)
        response_data = deepcopy(self.response_template)
        response_data['result'] = {'code': '000.400.010', 'description': 'failed repsonse'}
        mock_checkout_status.return_value = response_data
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        assert response.status_code == 400
        assert response.json()['error'] == (
            'Your payment was declined. No charges were made. '
            'You may try again or use a different payment method.'
        )
        self.processing_cart.refresh_from_db()
        assert self.processing_cart.status == Cart.Status.CANCELLED

    @pytest.mark.django_db
    @patch("hyperpay.client.HyperPayClient.get_checkout_status")
    def test_get_success_for_pending_payment(self, mock_checkout_status):
        self.client.force_login(self.user)
        response_data = deepcopy(self.response_template)

        # test when cart is in processing state and hyperpay response is pending
        response_data['result'] = {'code': '000.200.100', 'description': 'pending repsonse'}
        mock_checkout_status.return_value = response_data
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        assert response.status_code == 202
        assert response.json()['error'] == 'Payment status is still pending on Hyperpay.'
        self.processing_cart.refresh_from_db()
        assert self.processing_cart.status == Cart.Status.PAYMENT_PENDING, \
            'Cart status should be PENDING PAYMENT.'

        # test when cart is in payment_pending state and hyperpay response is still pending
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        assert response.status_code == 202
        assert response.json()['error'] == 'Payment status is still pending on Hyperpay.'
        self.processing_cart.refresh_from_db()
        assert self.processing_cart.status == Cart.Status.PAYMENT_PENDING

    @pytest.mark.django_db
    @patch("hyperpay.client.HyperPayClient.get_checkout_status")
    def test_get_for_invalid_hyperpay_checkout_response(self, mock_checkout_status):
        self.client.force_login(self.user)
        mock_checkout_status.return_value = {
            'invalid_field_in_response': 'response format is not right.',
            'merchantTransactionId': f'0011-{self.processing_cart.id}',
            'result': {'code': '000.100.110', 'description': 'success repsonse'},
            'id': '11223344'
        }
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    @pytest.mark.django_db
    @patch("hyperpay.client.requests.get")
    def test_get_with_success_payment_but_update_db_records_failed(self, mock_get):
        self.client.force_login(self.user)
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        self.processing_cart.status = Cart.Status.PROCESSING
        self.processing_cart.save()
        mock_response.json.return_value = deepcopy(self.response_template)
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        response = self.client.get(f'{self.url}?merchant_reference=1122')
        assert response.status_code == 200

        self.processing_cart.refresh_from_db()
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_successful.html')

    @pytest.mark.django_db
    @patch("hyperpay.client.HyperPayClient.get_checkout_status")
    def test_get_success_payment_with_invalid_merchant_ref(self, mock_checkout_status):
        self.client.force_login(self.user)
        response_data = deepcopy(self.response_template)
        response_data['merchantTransactionId'] = '11-invalid'
        mock_checkout_status.return_value = response_data
        response = self.client.get(f'{self.url}?merchant_reference=1122')
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')
