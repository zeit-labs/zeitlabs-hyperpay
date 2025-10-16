"""Hyperpay client."""

import base64
import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional

import requests
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
    RESULT_CODE_SUCCESSFULLY_CREATED_CHECKOUT = '000.200.100'
    SUCCESS_PROCESSED_TRANSACTION_REGEX = re.compile(r'^(000\.000\.|000\.100\.1|000\.[36]|000\.400\.[1][12]0)')

    SUCCESS_CODES_REGEX = re.compile(r'^(000\.000\.|000\.100\.1|000\.[36])')
    SUCCESS_MANUAL_REVIEW_CODES_REGEX = re.compile(r'^(000\.400\.0[^3]|000\.400\.[0-1]{2}0)')
    PENDING_CHANGEABLE_SOON_CODES_REGEX = re.compile(r'^(000\.200)')
    PENDING_NOT_CHANGEABLE_SOON_CODES_REGEX = re.compile(r'^(800\.400\.5|100\.400\.500)')

    def __init__(self, client_id: str, client_secret: str, base_url: str, slug: str) -> None:
        """Initialize client."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self.slug = slug
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None

    def _generate_access_token(self) -> None:
        """Generate an access token from HyperPay OAuth API."""
        credentials = f'{self.client_id}:{self.client_secret}'
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            'Authorization': f'Basic {encoded_credentials}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        data = {'grant_type': 'client_credentials', 'scope': 'pay'}

        try:
            response = requests.post(
                f'{self.base_url}/oauth2/v1/token',
                headers=headers,
                data=data,
                timeout=(5, 15),
            )
            response.raise_for_status()
            data = response.json()

            # subtract 30s buffer so we refresh slightly before expiry
            self.token_expiry = datetime.utcnow() + timedelta(seconds=int(data.get('expires_in', 0)) - 30)
            self.access_token = data.get('access_token')

            if not self.access_token:
                logger.error(f"HyperPay token response missing 'access_token': {data}")
                raise HyperPayException('Missing access token in HyperPay response.')

        except (requests.RequestException, ValueError) as exc:
            raise HyperPayException(f'Error generating token: {exc}') from exc

    def _ensure_token(self) -> None:
        """Ensure we have a valid token before making API calls."""
        if (
            not getattr(self, 'access_token', None) or
            not getattr(self, 'token_expiry', None) or
            (self.token_expiry is not None and datetime.utcnow() >= self.token_expiry)
        ):
            logger.info('Access token missing or expired. Generating new one.')
            self._generate_access_token()

    def create_checkout(self, payload: dict) -> Dict[str, str]:
        """Create a new checkout session in HyperPay."""
        self._ensure_token()
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.access_token}',
        }

        try:
            response = requests.post(
                f'{self.base_url}/payment-gateway/v1/checkout', headers=headers, json=payload, timeout=(5, 15)
            )
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

    def verify_checkout_status(self, params: dict) -> dict:
        """Verify checkout session status from HyperPay."""
        self._ensure_token()
        headers = {'Authorization': f'Bearer {self.access_token}'}

        try:
            response = requests.get(
                f'{self.base_url}/payment-gateway/v1/checkout/status',
                headers=headers,
                params=params,
                timeout=(5, 15),
            )
            data = response.json()
        except Exception as exc:
            raise HyperPayException(f'Error verifing checkout status. {exc}') from exc

        AuditLog.log(
            action=AuditLog.AuditActions.RECEIVED_RESPONSE,
            gateway=self.slug,
            context={'data': data}
        )

        result_code = data.get('result', {}).get('code')
        if result_code and self.SUCCESS_PROCESSED_TRANSACTION_REGEX.match(result_code):
            return data

        logger.error(f'HyperPay checkout status API failed: {data}')
        raise HyperPayException('Unable to verify checkout status.')

    def verify_webhook_callback_status(self, response_data: dict) -> PaymentStatus:
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
