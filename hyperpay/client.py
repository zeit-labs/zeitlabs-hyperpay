"""Hyperpay client."""

import logging
import re
from enum import Enum
from typing import Dict

import requests
from django.http import HttpResponse
from zeitlabs_payments.models import AuditLog

from hyperpay.exceptions import HyperPayBadGatewayResponse, HyperPayException

logger = logging.getLogger(__name__)


class PaymentStatus(Enum):
    SUCCESS = 0
    PENDING = 1
    FAILURE = 2


class HyperPayClient:
    """
    Handles API communication with Nelc-HyperPay.
    """
    PAYMENT_TYPE = 'DB'
    INTEGRITY = 'true'

    RESULT_CODE_SUCCESSFULLY_CREATED_CHECKOUT = '000.200.100'
    SUCCESS_PROCESSED_TRANSACTION_REGEX = re.compile(r'^(000\.000\.|000\.100\.1|000\.[36]|000\.400\.[1][12]0)')

    SUCCESS_CODES_REGEX = re.compile(r'^(000\.000\.|000\.100\.1|000\.[36])')
    SUCCESS_MANUAL_REVIEW_CODES_REGEX = re.compile(r'^(000\.400\.0[^3]|000\.400\.[0-1]{2}0)')
    PENDING_CHANGEABLE_SOON_CODES_REGEX = re.compile(r'^(000\.200)')
    PENDING_NOT_CHANGEABLE_SOON_CODES_REGEX = re.compile(r'^(800\.400\.5|100\.400\.500)')

    def __init__(  # pylint: disable=too-many-positional-arguments
            self, access_token: str, base_url: str, entity_id: str, slug: str, test_mode: str = None
    ) -> None:
        """Initialize client."""
        self.slug = slug
        self.access_token = access_token
        self.base_url = base_url
        self.entity_id = entity_id
        self.test_mode = test_mode

    @property
    def authentication_headers(self) -> dict:
        """
        Return the authentication headers.
        """
        return {
            'Authorization': 'Bearer {}'.format(self.access_token)
        }

    def record_response(self, response: HttpResponse) -> None:
        """Record api response in AuditLogs."""
        try:
            data = response.json()
        except ValueError:
            data = {'raw': response.text}

        data.update({'status': response.status_code})
        AuditLog.log(
            action=AuditLog.AuditActions.RECEIVED_RESPONSE,
            gateway=self.slug,
            context={'data': data}
        )

    def create_checkout(self, payload: dict) -> Dict[str, str]:
        """Create a new checkout session in HyperPay."""
        payload.update({
            'entityId': self.entity_id,
            'paymentType': self.PAYMENT_TYPE,
            'integrity': self.INTEGRITY
        })

        if self.test_mode:
            payload['testMode'] = self.test_mode
            payload['customParameters[3DS2_enrolled]'] = 'true'
            payload['customParameters[3DS2_flow]'] = 'challenge'

        try:
            response = requests.post(
                f'{self.base_url}/v1/checkouts', payload, headers=self.authentication_headers, timeout=(5, 15)
            )
            self.record_response(response)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise HyperPayException(f'Error creating a checkout. {exc}') from exc

        if 'result' not in data or 'code' not in data['result']:
            raise HyperPayException(
                'Error creating checkout. Invalid response from HyperPay.'
            )

        result_code = data['result']['code']
        if result_code != self.RESULT_CODE_SUCCESSFULLY_CREATED_CHECKOUT:
            raise HyperPayException(
                'Error creating checkout. HyperPay status code: {}'.format(result_code)
            )

        return {
            'checkout_id': data.get('id'),
            'nonce_id': data.get('ndc'),
            'integrity': data.get('integrity'),
        }

    def get_checkout_status(self, checkout_id: str) -> dict:
        """Verify checkout session status from HyperPay."""
        try:
            response = requests.get(
                f'{self.base_url}/v1/checkouts/{checkout_id}/payment?entityId={self.entity_id}',
                headers=self.authentication_headers,
                timeout=(5, 15),
            )
            self.record_response(response)
            data = response.json()
        except Exception as exc:
            raise HyperPayException(f'Error verifing checkout status. {exc}') from exc

        result_code = data.get('result', {}).get('code')
        if result_code and self.SUCCESS_PROCESSED_TRANSACTION_REGEX.match(result_code):
            return data

        logger.error(f'HyperPay checkout status API failed: {data}')
        raise HyperPayException('Unable to verify checkout status.')

    def verify_status(self, response_data: dict) -> PaymentStatus:
        """Verify status of callback response."""
        result_code = response_data.get('result', {}).get('code')
        payment_id = response_data.get('id')
        if not result_code or not payment_id:
            logger.warning(
                'Received HyperPay response without result code %s or payment id %s.',
                result_code,
                payment_id
            )
            raise HyperPayBadGatewayResponse(f'Missing result code: {result_code} or payment_id: {payment_id}.')

        if self.PENDING_CHANGEABLE_SOON_CODES_REGEX.search(result_code):
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
