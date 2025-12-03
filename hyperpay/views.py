"""Hyperpay views."""
import logging
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction as db_transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from zeitlabs_payments.models import Cart

from hyperpay.client import PaymentStatus
from hyperpay.exceptions import HyperPayException
from hyperpay.helpers import verify_success_response_with_cart
from hyperpay.processor import HyperPay

logger = logging.getLogger(__name__)


class HyperPayBaseView(View):
    """Hyperpay Base View."""

    @property
    def payment_processor(self) -> HyperPay:
        """Return processor."""
        return HyperPay()


@method_decorator(db_transaction.non_atomic_requests, name='dispatch')
@method_decorator(csrf_exempt, name='dispatch')
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

        data = {}
        data['checkout_id'] = checkout_id
        data['ecommerce_transaction_id'] = checkout_id
        data['ecommerce_status_url'] = reverse('hyperpay:status')
        data['ecommerce_error_url'] = reverse(
            'zeitlabs_payments:payment-error',
            args=[checkout_id]
        )
        data['ecommerce_success_url'] = reverse(
            'zeitlabs_payments:payment-success',
            args=[checkout_id]
        )
        data['ecommerce_max_attempts'] = self.MAX_ATTEMPTS
        data['ecommerce_wait_time'] = self.WAIT_TIME
        data['merchant_reference'] = checkout_id
        return render(request=request, template_name=self.template_name, context=data)


class HyperPayStatusView(LoginRequiredMixin, HyperPayBaseView):
    """View to check transaction and payment status."""

    def get(self, request: Any) -> JsonResponse:
        """Verify transaction status."""
        params = {
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

        checkout_id = request.GET.get('merchant_reference')
        try:
            data = self.payment_processor.client.get_checkout_status(checkout_id)
            cart = self.payment_processor.get_cart_from_reference(data['merchantTransactionId'])
            site = self.payment_processor.get_site_from_reference(data['merchantTransactionId'])
            if not cart or not site:
                return render(request, 'zeitlabs_payments/payment_error.html')

            if cart.status == Cart.Status.PROCESSING:
                cart.status = Cart.Status.PAYMENT_PENDING
                cart.save(update_fields=['status'])
        except HyperPayException as exc:
            logger.exception(
                f'Unable to verify checkout status from HyperPay with given checkout_id: {checkout_id} - {exc}'
            )
            return render(request, 'zeitlabs_payments/payment_error.html')

        status = self.payment_processor.client.verify_status(data)
        if status == PaymentStatus.FAILURE:
            logger.exception(
                f'Received failed response from hyperpay: {data}'
            )
            return render(request, 'zeitlabs_payments/payment_error.html')

        elif status == PaymentStatus.SUCCESS:
            try:
                verify_success_response_with_cart(data, cart)
            except HyperPayException:
                logger.exception('Hyperpay Error - Success response format check failed.')
                return render(request, 'zeitlabs_payments/payment_error.html')

            invoice = self.payment_processor.process_payment_and_update_records(
                cart=cart,
                data=data,
                request=request,
                transaction_id=data['id'],
                transaction_status=self.payment_processor.TRANSACTION_STATUS_SUCCESS,
                method=data['paymentBrand'],
                amount=data['amount'],
                currency=data['currency'],
                reason=data['result']['description'],
                record_webhook_event=False,
                site_id=site.id
            )
            if invoice:
                return JsonResponse({
                    'invoice': invoice.invoice_number,
                    'invoice_url': reverse(
                        'zeitlabs_payments:invoice',
                        args=[invoice.invoice_number]
                    )
                }, status=200)
            logger.exception(
                'Payment was successful but unable to update enrollment record in db,'
                f' please check audit logs for the cart: {cart.id}'
            )
            return render(request, 'zeitlabs_payments/payment_successful.html')
        else:
            return JsonResponse(
                data={'error': 'Payment status is still pending on Hyperpay.'},
                status=202
            )
