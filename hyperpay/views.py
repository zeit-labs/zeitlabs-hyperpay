"""Hyperpay views."""
import json
import logging
from typing import Any

from django.db import transaction as db_transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import IsAuthenticated
from zeitlabs_payments.exceptions import DuplicateTransactionError, InvalidCartError
from zeitlabs_payments.models import AuditLog, Cart, Invoice

from hyperpay.client import PaymentStatus
from hyperpay.exceptions import HyperPayException
from hyperpay.processor import HyperPay

logger = logging.getLogger(__name__)


class HyperPayBaseView(View):
    """Hyperpay Base View."""

    @property
    def payment_processor(self) -> HyperPay:
        """Return processor."""
        return HyperPay()


class HyperPayReturnView(HyperPayBaseView):
    """
    Hyperpay redirection view after payment.
    """

    template_name = 'zeitlabs_payments/wait_feedback.html'
    MAX_ATTEMPTS = 24
    WAIT_TIME = 5000

    def get(self, request: Any, *args: Any, **kwargs: Any) -> HttpResponse:
        """Handle the GET request from HyperPay after processing payment."""
        checkout_id = request.GET.get('id')
        if not checkout_id:
            logger.error('Missing checkout_id in GET parameters.')
            return render(request, 'zeitlabs_payments/payment_error.html')

        params = {
            'checkout_id': checkout_id,
            'payment_method': self.payment_processor.BRAND
        }
        try:
            data = self.payment_processor.client.verify_checkout_status(params)
            merchant_transaction_id = data.get('merchant_transaction_id', '')
            transaction_id = data.get('id', '')
        except HyperPayException as exc:
            logger.exception(
                f'Unable to verify checkout status from HyperPay with given checkout_id: {checkout_id} - {exc}'
            )
            return render(request, 'zeitlabs_payments/payment_error.html')

        data['ecommerce_transaction_id'] = transaction_id
        data['ecommerce_status_url'] = reverse('hyperpay:status')
        data['ecommerce_error_url'] = reverse(
            'zeitlabs_payments:payment-error',
            args=[transaction_id]
        )
        data['ecommerce_success_url'] = reverse(
            'zeitlabs_payments:payment-success',
            args=[transaction_id]
        )
        data['ecommerce_max_attempts'] = self.MAX_ATTEMPTS
        data['ecommerce_wait_time'] = self.WAIT_TIME
        data.update({'merchant_reference': merchant_transaction_id})
        return render(request=request, template_name=self.template_name, context=data)


class HyperPayStatusView(HyperPayBaseView):
    """View to check transaction and payment status."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Any) -> JsonResponse:
        """Verify transaction status."""
        params = {
            'transaction_id': request.GET.get('transaction_id'),
            'merchant_reference': request.GET.get('merchant_reference')
        }
        missing_fields = [key for key, value in params.items() if not value]
        if missing_fields:
            field_names = ', '.join(missing_fields).replace('_', ' ').title()
            logger.error(f'HyperPay Error! {field_names} is required to verify payment status.')
            return JsonResponse(
                data={'error': f'{field_names} is required to verify payment status.'},
                status=400
            )

        try:
            _, _, cart_id = params['merchant_reference'].split('-')
            cart = HyperPay().get_cart(cart_id)
        except (ValueError, InvalidCartError):
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.payment_processor.SLUG,
                context={
                    'cart_status': (
                        f'None, unable to retrieve cart from merchant transaction '
                        f"id {params['merchant_reference']}"),
                    'required_cart_state': Cart.Status.PROCESSING}
            )
            return JsonResponse(
                {
                    'error': f"merchant_reference: {params['merchant_reference']} is invalid. Unable to retrieve cart."
                }, status=404)

        status_code = {
            Cart.Status.PAID: 200,
            Cart.Status.PROCESSING: 202,
        }.get(cart.status, 404)

        if status_code == 200:
            invoice = Invoice.objects.filter(
                cart=cart,
                status=Invoice.InvoiceStatus.PAID,
            ).first()
            if invoice:
                if not invoice.related_transaction:
                    return JsonResponse(
                        data={'error': f'Invoice exists with ID: {invoice.id}, but related_transaction is None'},
                        status=400,
                    )

                if invoice.related_transaction.gateway_transaction_id != params['transaction_id']:
                    return JsonResponse(
                        data={
                            "error": (
                                f"Invoice exists with ID: {invoice.id}, but the transaction ID in the invoice "
                                f"({invoice.related_transaction}) does not match the transaction ID in the response "
                                f"({params['transaction_id']}) for cart ID: {cart.id}."
                            )
                        },
                        status=400,
                    )

                return JsonResponse(
                    {
                        'invoice': invoice.invoice_number,
                        'invoice_url': reverse(
                            'zeitlabs_payments:invoice',
                            args=[invoice.invoice_number]
                        )
                    }, status=200)

            error_msg = (
                f'Cart is in {Cart.Status.PAID} status but unable to retrieve related paid invoice.')
            logger.error(error_msg)
            data = {'error': error_msg}
            status_code = 202
        else:
            data = {'error': f'Looking for paid cart but cart is in status: {cart.status}.'}

        return JsonResponse(
            data=data,
            status=status_code
        )


class HyperPayWebhookView(HyperPayBaseView):
    """
    Callback endpoint for Hyperpay to notify about payment status.
    """

    @method_decorator(csrf_exempt)
    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Dispatch the request to the appropriate handler."""
        return super().dispatch(request, *args, **kwargs)

    def post(self, request: Any) -> HttpResponse:
        """Handle the POST request from HYperPay for payment status through webhook callback."""
        data = json.loads(request.body.decode('utf-8'))
        AuditLog.log(
            action=AuditLog.AuditActions.RECEIVED_RESPONSE,
            gateway=self.payment_processor.SLUG,
            context={'data': data}
        )
        status = self.payment_processor.client.verify_webhook_callback_status(data)

        if status != PaymentStatus.SUCCESS:
            logger.warning(f"Hyperpay payment unsuccessful or pending. Status: {status}, Data: {data}")
            return HttpResponse(status=200)

        # TODO: VERIFY REPONSE FORMAT. it hsould also check merchant transcation id format.
        _, site_id, cart_id = data['merchant_transaction_id'].split('-')

        try:
            cart = self.payment_processor.get_cart(cart_id)
        except InvalidCartError:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=None,
                gateway=self.payment_processor.SLUG,
                context={
                    'cart_status': (
                        f"None, unable to retrieve cart from merchant transaction "
                        f"id {data['merchant_transaction_id']}"
                    ),
                    'required_cart_state': Cart.Status.PROCESSING
                }
            )
            return HttpResponse(status=400)

        if cart.status != Cart.Status.PROCESSING:
            AuditLog.log(
                action=AuditLog.AuditActions.RESPONSE_INVALID_CART,
                cart=cart,
                gateway=self.payment_processor.SLUG,
                context={'cart_status': cart.status, 'required_cart_state': Cart.Status.PROCESSING}
            )
            logger.warning(f'Cart {cart.id} in invalid status: {cart.status} (expected: PROCESSING).')
            return HttpResponse(status=200)

        try:
            with db_transaction.atomic():
                logger.info(f'Recording payment transaction for cart {cart.id}.')
                transaction_record = self.payment_processor.handle_payment(
                    cart=cart,
                    user=request.user if request.user.is_authenticated else None,
                    transaction_status=self.payment_processor.TRANSACTION_STATUS_SUCCESS,
                    transaction_id=data['id'],
                    method=data['payment_brand'],
                    amount=data['amount'],
                    currency=data['currency'],
                    reason=data['result']['description'],
                    response=data,
                )
        except DuplicateTransactionError:
            AuditLog.log(
                action=AuditLog.AuditActions.DUPLICATE_TRANSACTION,
                cart=cart,
                gateway=self.payment_processor.SLUG,
                context={
                    'transaction_id': data['id'],
                    'cart_status': cart.status
                }
            )
            return HttpResponse(status=200)
        except Exception as e:  # pylint: disable=broad-exception-caught
            AuditLog.log(
                action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK,
                cart=cart,
                gateway=self.payment_processor.SLUG,
                context={
                    'transaction_id': data['id'],
                    'cart_id': cart.id,
                    'site_id': site_id
                }
            )
            logger.error(f'Payment transaction failed and rolled back for cart {cart.id}: {str(e)}')
            return HttpResponse(status=200)

        try:
            cart.refresh_from_db()
            invoice = self.payment_processor.create_invoice(cart, request, transaction_record)
            self.payment_processor.fulfill_cart(cart)
            AuditLog.log(
                action=AuditLog.AuditActions.CART_FULFILLED,
                cart=cart,
                gateway=self.payment_processor.SLUG,
                context={}
            )
            logger.info(f'Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.')
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f'Failed to fulfill cart {cart.id} or to create invoice: {str(e)}')
        return HttpResponse(status=200)
