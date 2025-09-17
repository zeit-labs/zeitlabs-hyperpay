import re
import logging
import json
from django.http import HttpResponse, JsonResponse
from django.views import View
from hyperpay.processor import HyperPay    
from enum import Enum
from .exceptions import HyperPayBadGatewayResponse
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import render

from zeitlabs_payments.models import Transaction, AuditLog, Cart, Invoice
logger = logging.getLogger(__name__)
from django.urls import reverse

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction as db_transaction



class PaymentStatus(Enum):
    SUCCESS = 0
    PENDING = 1
    FAILURE = 2


class HyperPayBaseView(View):
    """Payfort Base View."""

    @property
    def payment_processor(self) -> HyperPay:
        """Return processor."""
        return HyperPay()

class HyperPayReturnView(HyperPayBaseView):
    """
    Payfort redirection view after payment.
    """

    template_name = 'zeitlabs_payments/wait_feedback.html'
    MAX_ATTEMPTS = 24
    WAIT_TIME = 5000

    def get(self, request, *args, **kwargs):
        checkout_id = request.GET.get('id')
        params = {
            "checkout_id": checkout_id,
            "payment_method": self.payment_processor.BRAND
        }
        try:
            data = self.payment_processor.client.verify_checkout_status(params) # update it to raise exception
            AuditLog.log(
                action=AuditLog.AuditActions.RECEIVED_RESPONSE,
                gateway=self.payment_processor.SLUG,
                context={'data': data}
            )
            merchant_transaction_id = data.get('merchant_transaction_id')
        except:
            logger.error(f'Unable to get merchant_transaction_id from hyperpay with given checkout_id: {checkout_id}')
            return render(request, 'zeitlabs_payments/payment_error.html')

        merchant_transaction_id = merchant_transaction_id.split("-", 1)[1]
        data['ecommerce_transaction_id'] = merchant_transaction_id
        data['ecommerce_status_url'] = reverse('hyperpay:status')
        data['ecommerce_error_url'] = reverse(
            'zeitlabs_payments:payment-error',
            args=[merchant_transaction_id]
        )
        data['ecommerce_success_url'] = reverse(
            'zeitlabs_payments:payment-success',
            args=[merchant_transaction_id]
        )
        data['ecommerce_max_attempts'] = self.MAX_ATTEMPTS
        data['ecommerce_wait_time'] = self.WAIT_TIME
        return render(request=request, template_name=self.template_name, context=data)


class HyperPayStatusView(HyperPayBaseView):
    """View to check transaction and payment status."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> JsonResponse:
        """Verify transaction status."""
        transaction_id = request.GET.get('transaction_id')

        if not transaction_id:
            logger.error(f'Payfort Error! transaction_id is required to verify payment status.')
            return JsonResponse(
                data={'error': f'transaction_id is required to verify payment status.'},
                status=400
            )

        try:
            transaction = Transaction.objects.get(gateway_transaction_id=transaction_id, gateway=self.payment_processor.SLUG)
            if not transaction.cart:
                AuditLog.log(
                    action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                    cart=None,
                    gateway=self.payment_processor.SLUG,
                    context={'cart_status': f"Transaction {transaction_id} exists without cart'", 'required_cart_state': Cart.Status.PROCESSING}
                )
                return JsonResponse(
                    {
                        "error": f"Transaction {transaction_id} exists, but the associated cart could not be found.",
                    }, status=404)
        except (Transaction.DoesNotExist):
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': 'Unable to get cart as transaction does not exist', 'required_cart_state': Cart.Status.PROCESSING}
            )
            return JsonResponse(
                {
                    'error': f"Transaction with: {transaction_id} does not exist, unable to get cart"
                }, status=404)

        status_code = {
            Cart.Status.PAID: 200,
            Cart.Status.PROCESSING: 204,
        }.get(transaction.cart.status, 404)

        if status_code == 200:
            invoice = Invoice.objects.filter(
                cart=transaction.cart,
                status=Invoice.InvoiceStatus.PAID,
                related_transaction__gateway_transaction_id=transaction_id,
            ).first()
            if invoice:
                return JsonResponse(
                    {
                        'invoice': invoice.invoice_number,
                        'invoice_url': reverse(
                            'zeitlabs_payments:invoice',
                            args=[invoice.invoice_number]
                        )
                    }, status=200)

            error_msg = f'Cart is in {Cart.Status.PAID} status, unable to retrieve invoice with given transaction id.'
            logger.error(error_msg)
            data = {'error': error_msg}
            status_code = 204
        else:
            data = {'error': f'cart is in status: {transaction.cart.status}.'}

        return JsonResponse(
            data=data,
            status=status_code
        )

class HyperPayWebhookView(HyperPayBaseView):
    """
    Callback endpoint for PayFort to notify about payment status.
    """
    SUCCESS_CODES_REGEX = re.compile(r'^(000\.000\.|000\.100\.1|000\.[36])')
    SUCCESS_MANUAL_REVIEW_CODES_REGEX = re.compile(r'^(000\.400\.0[^3]|000\.400\.[0-1]{2}0)')
    PENDING_CHANGEABLE_SOON_CODES_REGEX = re.compile(r'^(000\.200)')
    PENDING_NOT_CHANGEABLE_SOON_CODES_REGEX = re.compile(r'^(800\.400\.5|100\.400\.500)')
    PENDING_STATUS_URL_NAME = 'hyperpay:status-check'
    PENDING_STATUS_PAGE_TITLE = 'HyperPay - Credit card - pending'
    
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        """Dispatch the request to the appropriate handler."""
        return super().dispatch(request, *args, **kwargs)


    def _verify_status(self, response_data):
        result_code = response_data.get('result', {}).get('code')
        payment_id = response_data.get('id')
        if not result_code or not payment_id:
            logger.warning(
                'Received HyperPay response without result code %s or payment id %s.',
                result_code,
                payment_id
            )
            raise HyperPayBadGatewayResponse(f'Missing result code: {result_code} or payment_id: {payment_id}.')
        elif self.PENDING_CHANGEABLE_SOON_CODES_REGEX.search(result_code):
            logger.warning(
                'Received a pending status code %s from HyperPay for payment id %s.',
                result_code,
                response_data['id']
            )
            status = PaymentStatus.PENDING
        elif self.PENDING_NOT_CHANGEABLE_SOON_CODES_REGEX.search(result_code):
            logger.warning(
                'Received a pending status code %s from HyperPay for payment id %s. As this can change '
                'after several days, treating it as a failure.',
                result_code,
                response_data['id']
            )
            status = PaymentStatus.FAILURE
        elif self.SUCCESS_CODES_REGEX.search(result_code):
            logger.info(
                'Received a success status code %s from HyperPay for payment id %s.',
                result_code,
                response_data['id']
            )
            status = PaymentStatus.SUCCESS
        elif self.SUCCESS_MANUAL_REVIEW_CODES_REGEX.search(result_code):
            logger.error(
                'Received a success status code %s from HyperPay which requires manual verification for payment id %s.'
                'Treating it as a failed transaction.',
                result_code,
                response_data['id']
            )

            # This is a temporary change till we get clarity on whether this should be treated as a failure.
            status = PaymentStatus.FAILURE
        else:
            logger.error(
                'Received a rejection status code %s from HyperPay for payment id %s',
                result_code,
                response_data['id']
            )
            status = PaymentStatus.FAILURE
        return status

    def post(self, request):
        """Handle the POST request from HYperPay for payment status through webhook callback."""
        # data = request.POST.dict()
        data = json.loads(request.body.decode("utf-8"))
        AuditLog.log(
            action=AuditLog.AuditActions.RECEIVED_RESPONSE,
            gateway=self.payment_processor.SLUG,
            context={'data': data}
        )

        status = self._verify_status(data)

        if status != PaymentStatus.SUCCESS:
            logger.warning(f"PayFort payment unsuccessful or pending. Status: {status}, Data: {data}")
            return HttpResponse(status=200)

        # verify_response_format(data)
        transaction_id = data['merchant_transaction_id'].split("-", 1)[1]
        try:
            transaction = Transaction.objects.get(
                gateway_transaction_id=transaction_id,
                gateway=self.payment_processor.SLUG,
                type=Transaction.TransactionType.PAYMENT,
                status='Pending'
            )
            if not transaction.cart:
                AuditLog.log(
                    action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                    cart=None,
                    gateway=self.payment_processor.SLUG,
                    context={'cart_status': f"Transaction {transaction_id} exists without cart", 'required_cart_state': Cart.Status.PROCESSING}
                )
                return HttpResponse(status=200)
        except Transaction.DoesNotExist:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': 'Unable to get cart as transaction does not exist', 'required_cart_state': Cart.Status.PROCESSING}
            )
            return HttpResponse(status=200)
        

        if transaction.cart.status != Cart.Status.PROCESSING:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=transaction.cart,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': transaction.cart.status, 'required_cart_state': Cart.Status.PROCESSING}
            )
            logger.warning(f'Cart {transaction.cart.id} in invalid status: {transaction.cart.status} (expected: PROCESSING).')
            return HttpResponse(status=200)

        try:
            with db_transaction.atomic():
                logger.info(f'Updatinf payment transaction for cart {transaction.cart.id}.')
                transaction_record = self.payment_processor.handle_payment(
                    cart=transaction.cart,
                    user=request.user if request.user.is_authenticated else None,
                    transaction_status='Success',
                    transaction_id=transaction_id,
                    method=data['payment_brand'],
                    amount=data['amount'],
                    currency=data['currency'],
                    reason=data['result']['description'],
                    response=data,
                    pending_transaction=transaction,
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            AuditLog.log(
                action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK,
                cart=transaction.cart,
                gateway=self.payment_processor.SLUG,
                context={
                    'transaction_id': transaction_id,
                    'cart_id': transaction.cart.id,
                    'site_id': ''
                }
            )
            logger.error(f'Payment transaction failed and rolled back for cart {transaction.cart.id}: {str(e)}')
            return HttpResponse(status=200)

        try:
            transaction.cart.refresh_from_db()
            invoice = self.payment_processor.create_invoice(transaction.cart, request, transaction_record)
            self.payment_processor.fulfill_cart(transaction.cart)
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                cart=transaction.cart,
                gateway=self.payment_processor.SLUG,
                context={}
            )
            logger.info(f'Successfully fulfilled cart {transaction.cart.id} and created invoice {invoice.id}.')
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f'Failed to fulfill cart {transaction.cart.id} or to create invoice: {str(e)}')
        return HttpResponse(status=200)