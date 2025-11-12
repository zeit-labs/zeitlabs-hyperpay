
"""Test views for hyperpay provider"""
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction, WebhookEvent

from hyperpay.exceptions import HyperPayException
from hyperpay.processor import HyperPay
from hyperpay.views import HyperPayWebhookView

User = get_user_model()


class HyperPayReturnView(TestCase):
    """HyperpayReturnView Tests."""

    @patch('hyperpay.client.HyperPayClient.verify_checkout_status')
    def test_get_success(self, processor_client_verify_status):
        payment_id = '11223344'
        checkout_id = '1234'
        processor_client_verify_status.return_value = {
            'result': {'code': '000.000.000'}, 'merchant_transaction_id': 'ABCD-00011-12', 'id': payment_id
        }
        response = self.client.get(f"{reverse('hyperpay:return')}?id={checkout_id}")
        self.assertTemplateUsed(response, 'zeitlabs_payments/wait_feedback.html')
        assert response.context['ecommerce_transaction_id'] == payment_id
        assert response.context['ecommerce_status_url'] == reverse('hyperpay:status')
        assert response.context['ecommerce_error_url'] == reverse(
            'zeitlabs_payments:payment-error',
            args=[payment_id]
        )
        assert response.context['ecommerce_success_url'] == reverse(
            'zeitlabs_payments:payment-success',
            args=[payment_id]
        )
        assert response.context['ecommerce_max_attempts'] == 24
        assert response.context['ecommerce_wait_time'] == 5000

    def test_get_missing_checkout_id(self):
        """Should render payment_error.html when checkout_id is missing."""
        response = self.client.get(reverse('hyperpay:return'))
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')

    @patch('hyperpay.client.HyperPayClient.verify_checkout_status')
    def test_get_hyperpay_exception(self, processor_client_verify_status):
        """Should render payment_error.html when HyperPayException is raised."""
        processor_client_verify_status.side_effect = HyperPayException('Test failure')
        response = self.client.get(f"{reverse('hyperpay:return')}?id=1234")
        self.assertTemplateUsed(response, 'zeitlabs_payments/payment_error.html')


@pytest.mark.django_db
class HyperPayStatusViewTest(TestCase):
    """Tests for HyperPayStatusView"""

    def setUp(self):
        """Setup"""
        self.payment_processor = HyperPay()
        self.user = User.objects.create(username='test-user')
        self.processing_cart = Cart.objects.create(user=self.user, status=Cart.Status.PROCESSING)
        self.unknown_cart = Cart.objects.create(user=self.user, status='UNKNOWN')
        self.paid_cart = Cart.objects.create(user=self.user, status=Cart.Status.PAID)
        self.url = reverse('hyperpay:status')

    def login_user(self, user):
        """Helper to login user"""
        self.client.force_login(user)

    def test_unauthorized(self):
        """Verify that the view returns 404 when the user is not authenticated"""
        response = self.client.get(self.url, data={})
        self.assertEqual(response.status_code, 400)

    def test_missing_merchant_identifier(self):
        response = self.client.get(f'{self.url}?transaction_id=something')
        assert response.status_code == 400
        assert response.json()['error'] == 'Merchant Reference is required to verify payment status.'

    def test_missing_transaction_id(self):
        """Missing transaction id"""
        response = self.client.get(f'{self.url}?merchant_reference=something')
        assert response.status_code == 400
        assert response.json()['error'] == 'Transaction Id is required to verify payment status.'

    def test_invalid_merchant_reference_format(self) -> None:
        """
        Test that posting with an invalid cart ID in merchant_reference raises HyperpayException.
        :return: None
        """
        response = self.client.get(f'{self.url}?merchant_reference=invalid-format&transaction_id=112233')
        assert response.status_code == 404
        assert response.json()['error'] == 'merchant_reference: invalid-format is invalid. Unable to retrieve cart.'

    def test_invalid_cart_in_merchant_ref(self) -> None:
        """
        Test that posting with an invalid cart ID in merchant_reference raises HyperpayException.
        :return: None
        """
        response = self.client.get(f'{self.url}?merchant_reference=ABC-0001-1111&transaction_id=112233')
        assert response.status_code == 404
        assert response.json()['error'] == 'merchant_reference: ABC-0001-1111 is invalid. Unable to retrieve cart.'

    def test_paid_cart_with_invoice_and_transaction(self):
        """Cart is PAID and invoice exists and transaction status is sucess"""
        self.login_user(self.user)
        test_transaction_id = '11223344'
        transaction = Transaction.objects.create(
            gateway_transaction_id=test_transaction_id,
            gateway='hyperpay',
            amount='1000',
        )
        Invoice.objects.create(
            cart=self.paid_cart,
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='DEV-100',
            related_transaction=transaction,
            total=transaction.amount,
            gross_total=transaction.amount,
        )

        response = self.client.get(
            f'{self.url}?transaction_id={test_transaction_id}&merchant_reference=ABC-0002-{self.paid_cart.id}'
        )

        assert response.status_code == 200
        data = response.json()
        assert data['invoice'] == 'DEV-100'
        assert data['invoice_url'] == reverse(
            'zeitlabs_payments:invoice',
            args=['DEV-100']
        )

    def test_paid_cart_with_invoice_and_mismatched_transaction_id(self):
        """Cart is PAID and invoice exists and transaction status is sucess"""
        self.login_user(self.user)
        test_transaction_id = '11223344'
        other_transaction_id = '9999'
        transaction = Transaction.objects.create(
            gateway_transaction_id=test_transaction_id,
            gateway='hyperpay',
            amount='1000',
        )
        Invoice.objects.create(
            cart=self.paid_cart,
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='DEV-100',
            related_transaction=transaction,
            total=transaction.amount,
            gross_total=transaction.amount,
        )

        response = self.client.get(
            f'{self.url}?transaction_id={other_transaction_id}&merchant_reference=ABC-0002-{self.paid_cart.id}'
        )

        assert response.status_code == 400
        assert response.json()['error'] == (
            'Invoice exists with ID: 1, but the transaction ID in the invoice (Transaction object (1)) does '
            'not match the transaction ID in the response (9999) for cart ID: 3.'
        )

    def test_paid_cart_with_invoice_but_without_transaction(self):
        """Cart is PAID and invoice exists and transaction status is sucess"""
        self.login_user(self.user)
        test_transaction_id = '11223344'
        Invoice.objects.create(
            cart=self.paid_cart,
            status=Invoice.InvoiceStatus.PAID,
            invoice_number='DEV-100',
            related_transaction=None,
            total='1000',
            gross_total='1000',
        )

        response = self.client.get(
            f'{self.url}?transaction_id={test_transaction_id}&merchant_reference=ABC-0002-{self.paid_cart.id}'
        )

        assert response.status_code == 400
        assert response.json()['error'] == 'Invoice exists with ID: 1, but related_transaction is None'

    def test_paid_cart_without_invoice(self):
        """Trasanction is success, Cart is PAID but no invoice found"""
        self.login_user(self.user)
        test_transaction_id = '11223344'
        response = self.client.get(
            f'{self.url}?transaction_id={test_transaction_id}&merchant_reference=ABC-0002-{self.paid_cart.id}'
        )
        assert response.status_code == 202

    def test_processing_cart(self):
        """Cart in PROCESSING status"""
        self.login_user(self.user)
        test_transaction_id = '11223344'
        response = self.client.get(
            f'{self.url}?transaction_id={test_transaction_id}&merchant_reference=ABC-0002-{self.processing_cart.id}'
        )
        assert response.status_code == 202
        assert response.json()['error'] == 'Looking for paid cart but cart is in status: processing.'

    def test_unknown_cart_status(self):
        """Cart in unknown status"""
        self.login_user(self.user)
        test_transaction_id = '11223344'
        response = self.client.get(
            f'{self.url}?transaction_id={test_transaction_id}&merchant_reference=ABC-0002-{self.unknown_cart.id}'
        )
        assert response.status_code == 404
        data = response.json()
        assert data['error'] == 'Looking for paid cart but cart is in status: UNKNOWN.'


@pytest.mark.django_db
class HyperPayWebhookTestView(TestCase):
    """Hyperpay webhook test case."""

    def setUp(self) -> None:
        """
        Set up test data for the hyperpay webhook callback.
        """
        self.payment_processor = HyperPay()
        self.user = User.objects.create(username='test-user')
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
        self.fake_course_mode = Mock()
        self.fake_course_mode.course.id = self.course_item.item_ref_id
        self.url = reverse('hyperpay:webhook')

        self.valid_response = {
            "id": "123456789",
            "payment_type": "DB",
            "payment_brand": "VISA",
            "amount": "1000.00",
            "currency": "SAR",
            "merchant_transaction_id": f"ABCD-1-{self.cart.id}",
            "result": {
                "code": "000.100.110",
                "description": "Request successfully processed."
            },
            "card": {
                "bin": "411111",
                "last4_digits": "1111",
                "holder": "John",
                "expiry_month": "11",
                "expiry_year": "2027",
                "type": "DEBIT"
            },
            "customer": {
                "given_name": "jhon",
                "email": "jhon@example.com"
            },
            "ndc": "abcd.1234",
            "tracking_id": "11-22-33-44"
        }
        self.request_factory = RequestFactory()

    @patch('hyperpay.views.logger.warning')
    def test_post_for_unsuccessful_response(self, mock_logger) -> None:
        """
        Test that posting with an invalid cart ID in merchant_reference raises HyperpayException.
        :return: None
        """
        data = self.valid_response.copy()
        data.update({
            'result': {
                'code': '800.100.100',
                'description': 'transaction declined for unknown reason'
            }
        })
        request = self.request_factory.post(self.url, data, content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)
        mock_logger.assert_called_with(
            f"Hyperpay payment unsuccessful or pending. Status: PaymentStatus.FAILURE, Data: {data}"
        )
        assert response.status_code == 200

    def test_post_for_invalid_cart_in_merchant_ref(self) -> None:
        """
        Test that posting with an invalid cart ID in merchant_reference raises HyperpayException.
        :return: None
        """
        data = self.valid_response.copy()
        data.update({'merchant_transaction_id': 'ABCD-1-10000'})
        request = self.request_factory.post(self.url, data, content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)
        assert response.status_code == 400

    def test_post_for_cart_not_in_processing_state(self) -> None:
        """
        Test that posting with a cart not in PROCESSING state raises HyperpayException.
        :return: None
        """
        self.cart.status = Cart.Status.PENDING
        self.cart.save()
        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user
        assert not AuditLog.objects.filter(
            gateway='hyperpay',
            action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
        ).exists()
        response = HyperPayWebhookView.as_view()(request)
        assert AuditLog.objects.filter(gateway='hyperpay', action=AuditLog.AuditActions.RESPONSE_INVALID_CART).exists()
        assert response.status_code == 200

    @patch('hyperpay.views.logger.error')
    @patch("zeitlabs_payments.cart_handler.CourseMode")
    def test_post_for_success_payment_enroll_error_no_course_mode(self, mock_course_mode, mock_logger) -> None:
        """
        Test successful payment but course mode missing, triggers error logging and error page.
        :param mock_logger: mocked logger.error function
        :param mock_render: mocked render function
        :return: None
        """
        mock_course_mode.DoesNotExist = Exception
        mock_course_mode.objects.get.side_effect = Exception("CourseMode not found")
        assert not Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)

        assert Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after successful payment'

        mock_logger.assert_called_with(
            f'Failed to fulfill cart {self.cart.id} or to create invoice: CourseMode not found'
        )
        assert response.status_code == 200

    def test_post_success_for_rolled_back_of_tables_on_handle_payment_error(self) -> None:
        """
        Test successful payment but course mode missing, triggers error logging and error page.
        :param mock_logger: mocked logger.error function
        :param mock_render: mocked render function
        :return: None
        """
        assert not Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert not WebhookEvent.objects.filter(
            gateway='hyperpay',
            event_type='direct-feedback'
        ).exists(), \
            'WebhookEvent should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user

        with patch(
            'zeitlabs_payments.providers.base.WebhookEvent.objects.create',
            side_effect=Exception('Unknown exception')
        ):

            response = HyperPayWebhookView.as_view()(request)

            assert not Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
                'Transaction should not exist after test'
            assert not WebhookEvent.objects.filter(
                gateway='hyperpay',
                event_type='direct-feedback'
            ).exists(), \
                'WebhookEvent should not exist after test'
            assert self.cart.status == Cart.Status.PROCESSING, \
                'Cart should not be changed and should be in PROCESSING state'
            assert response.status_code == 200

    def test_post_success_for_duplicate_transaction(self) -> None:
        """
        Test successful payment but transaction already there with transaction_id received in response.
        """
        Transaction.objects.create(
            gateway='hyperpay',
            cart=self.cart,
            gateway_transaction_id=self.valid_response['id'],
            amount=100
        )

        assert not AuditLog.objects.filter(
            action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
            cart=self.cart,
            gateway='hyperpay'
        ).exists()
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)

        assert AuditLog.objects.filter(
            action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
            cart=self.cart,
            gateway='hyperpay'
        ).exists()
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart status should not be changed.'
        assert response.status_code == 200

    @patch('hyperpay.views.logger.error')
    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    @patch("zeitlabs_payments.cart_handler.CourseMode")
    def test_post_for_success_payment_paid_course_with_unsuccessful_enrollment(
        self, mock_course_mode, mock_enroll, mock_logger
    ) -> None:
        """
        Test payment success but enrollment fails, logs exception and shows error page.
        :param mock_enroll: mocked CourseEnrollment.enroll method
        :param mock_logger: mocked logger.exception function
        :param mock_render: mocked render function
        :return: None
        """
        mock_course_mode.objects.get.return_value = self.fake_course_mode
        mock_enroll.side_effect = Exception('Unexpected error during enrollment')
        assert not Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'

        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)

        assert Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after successful payment'

        mock_logger.assert_called_with(
            f'Failed to fulfill cart {self.cart.id} or to create invoice: Unexpected error during enrollment'
        )
        assert response.status_code == 200

    @pytest.mark.django_db
    @patch('zeitlabs_payments.cart_handler.CourseEnrollment.enroll')
    @patch("zeitlabs_payments.cart_handler.CourseMode")
    def test_post_for_successful_payment(
        self, mock_course_mode, mock_enroll  # pylint: disable=unused-argument
    ) -> None:
        """
        Test the full successful payment flow and enrollment.
        :param mock_render: mocked render function
        :return: None
        """
        mock_course_mode.objects.get.return_value = self.fake_course_mode
        assert not Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'
        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)

        assert Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after payment'
        assert response.status_code == 200

    @pytest.mark.django_db
    @patch('hyperpay.views.logger.error')
    def test_post_for_success_payment_cart_with_unsupported_item(self, mock_logger) -> None:
        """
        Test successful payment but cart contains unsupported item, triggers error logging.
        :param mock_logger: mocked logger.exception function
        :param mock_render: mocked render function
        :return: None
        """
        assert not Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should not exist before test'
        assert self.cart.status == Cart.Status.PROCESSING, \
            'Cart should be in PROCESSING state'
        unsupported_item = CatalogueItem.objects.create(sku='abcd', type='unsupported', price=50)
        self.cart.items.all().delete()
        self.cart.items.create(
            catalogue_item=unsupported_item,
            original_price=unsupported_item.price,
            final_price=unsupported_item.price,
        )

        request = self.request_factory.post(self.url, self.valid_response.copy(), content_type="application/json")
        request.user = self.user
        response = HyperPayWebhookView.as_view()(request)

        assert Transaction.objects.filter(gateway='hyperpay', cart=self.cart).exists(), \
            'Transaction should exist after payment'
        self.cart.refresh_from_db()
        assert self.cart.status == Cart.Status.PAID, \
            'Cart status should be PAID after payment'

        mock_logger.assert_called_with(
            f'Failed to fulfill cart {self.cart.id} or to create invoice: Unsupported catalogue item type: unsupported'
        )
        assert response.status_code == 200
