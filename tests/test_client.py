"""Tests for the HyperPayClient class and related payment flows."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from ddt import data, ddt, unpack
from django.test import TestCase
from requests import HTTPError
from zeitlabs_payments.models import AuditLog

from hyperpay.client import HyperPayClient, PaymentStatus
from hyperpay.exceptions import HyperPayBadGatewayResponse, HyperPayException


@ddt
@pytest.mark.django_db
class TestHyperPayClient(TestCase):
    """Tests for the HyperPayClient."""

    def setUp(self):
        """Set up reusable test data."""
        self.client = HyperPayClient(
            client_id='client_id',
            client_secret='client_secret',
            base_url='https://fake.hyperpay.com',
            slug='hyperpay',
        )

    @data(
        ('Missing token', None, datetime.utcnow() + timedelta(hours=1), True),
        ('Missing expiry', 'token_123', None, True),
        ('Expired token', 'token_123', datetime.utcnow() - timedelta(seconds=1), True),
        ('Valid token', 'token_123', datetime.utcnow() + timedelta(minutes=30), False),
    )
    @patch.object(HyperPayClient, "_generate_access_token")
    def test_ensure_token_variants(self, case_data, mock_generate):
        """Ensure token is (re)generated only when needed."""
        usecase, access_token, token_expiry, should_generate = case_data
        self.client.access_token = access_token
        self.client.token_expiry = token_expiry

        self.client._ensure_token()  # pylint: disable=protected-access

        if should_generate:
            assert mock_generate.called, f"{usecase}: Expected token generation."
        else:
            assert not mock_generate.called, f"{usecase}: Unexpected token generation."

    @patch('requests.post')
    def test_generate_access_token_success(self, mock_post):
        """It should correctly generate and set access token."""
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {'access_token': 'abc123', 'expires_in': 3600}
        mock_post.return_value = mock_response

        self.client._generate_access_token()  # pylint: disable=protected-access

        assert self.client.access_token == 'abc123'
        assert isinstance(self.client.token_expiry, datetime)

    @patch('requests.post')
    def test_generate_access_token_failure_no_token_in_response(self, mock_post):
        """It should raise HyperPayException if token not in response."""
        mock_resp = Mock()
        mock_resp.raise_for_status = Mock()
        mock_resp.json.return_value = {'error': 'abc123'}
        mock_post.return_value = mock_resp

        with pytest.raises(HyperPayException):
            self.client._generate_access_token()  # pylint: disable=protected-access

    @patch('requests.post')
    def test_generate_access_token_api_failure(self, mock_post):
        """It should raise HyperPayException if API call fails."""
        mock_resp = Mock()
        mock_resp.raise_for_status.side_effect = HTTPError('API down')
        mock_post.return_value = mock_resp

        with pytest.raises(HyperPayException):
            self.client._generate_access_token()  # pylint: disable=protected-access

    @patch('requests.post')
    def test_create_checkout_success(self, mock_post):
        """It should successfully create a checkout."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {
            'id': 'chk_123',
            'ndc': 'nonce_123',
            'integrity': 'int_123',
            'result': {'code': '000.200.100'},
        }
        mock_post.return_value = mock_response

        result = self.client.create_checkout({'amount': '10.00'})
        assert result == {
            'checkout_id': 'chk_123',
            'nonce_id': 'nonce_123',
            'integrity': 'int_123',
        }

    @patch('requests.post')
    def test_create_checkout_invalid_response(self, mock_post):
        """It should raise HyperPayException for invalid response format."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {'unexpected': 'format'}
        mock_post.return_value = mock_response

        with pytest.raises(HyperPayException):
            self.client.create_checkout({'amount': '10.00'})

    @patch('requests.post')
    def test_create_checkout_http_error(self, mock_post):
        """Should raise HyperPayException if raise_for_status() fails."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = HTTPError('500 Server Error')
        mock_post.return_value = mock_response

        with pytest.raises(HyperPayException) as excinfo:
            self.client.create_checkout({'amount': '100.00', 'currency': 'USD'})

        assert 'Error creating a checkout' in str(excinfo.value)
        assert '500 Server Error' in str(excinfo.value)

    @patch('requests.post')
    def test_create_checkout_invalid_json(self, mock_post):
        """Should raise HyperPayException if response.json() fails (invalid format)."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.side_effect = ValueError('No JSON object could be decoded')
        mock_post.return_value = mock_response

        with pytest.raises(HyperPayException) as excinfo:
            self.client.create_checkout({'amount': '100.00', 'currency': 'USD'})

        assert 'Error creating a checkout' in str(excinfo.value)
        assert 'No JSON object could be decoded' in str(excinfo.value)

    @patch('requests.post')
    def test_create_checkout_unsuccessful_result_code(self, mock_post):
        """Should raise HyperPayException if result code is not successful."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            'id': 'abc123',
            'ndc': 'xyz987',
            'integrity': 'hash123',
            'result': {'code': '200.300.400'},
        }
        mock_post.return_value = mock_response

        with pytest.raises(HyperPayException) as excinfo:
            self.client.create_checkout({'amount': '100.00', 'currency': 'USD'})

        assert 'Error creating checkout. HyperPay status code:' in str(excinfo.value)
        assert '200.300.400' in str(excinfo.value)

    @patch('requests.get')
    def test_verify_checkout_status_success_creates_auditlog(self, mock_get):
        """It should verify checkout and create an AuditLog."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)
        assert not AuditLog.objects.filter(gateway='hyperpay').exists()

        mock_response = Mock()
        mock_response.json.return_value = {'result': {'code': '000.000.000'}}
        mock_get.return_value = mock_response

        resp_data = self.client.verify_checkout_status({'checkout_id': 'chk_123'})
        assert resp_data['result']['code'] == '000.000.000'

        assert AuditLog.objects.filter(
            gateway='hyperpay',
            action='received_gateway_response',
        ).exists()

    @patch('requests.get')
    def test_verify_checkout_status_failure(self, mock_get):
        """It should raise exception for failed status."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)

        mock_response = Mock()
        mock_response.json.return_value = {'result': {'code': '999.999.999'}}
        mock_get.return_value = mock_response

        with pytest.raises(HyperPayException):
            self.client.verify_checkout_status({'checkout_id': 'chk_123'})

    @data(
        ('HTTP error', Exception('500 Server Error'), '500 Server Error'),
        ('Invalid JSON', ValueError('No JSON object could be decoded'), 'No JSON object could be decoded'),
        ('Generic exception', RuntimeError('Something broke'), 'Something broke'),
    )
    @unpack
    @patch("requests.get")
    def test_verify_checkout_status_exceptions(self, usecase, exception_obj, expected_msg, mock_get):
        """Should raise HyperPayException for different error cases."""
        self.client.access_token = 'abc'
        self.client.token_expiry = datetime.utcnow() + timedelta(hours=1)
        mock_response = Mock()
        mock_response.json.side_effect = exception_obj
        mock_get.return_value = mock_response

        with pytest.raises(HyperPayException) as excinfo:
            self.client.verify_checkout_status({'checkout_id': 'test123'})

        assert 'Error verifing checkout status' in str(excinfo.value)
        assert expected_msg in str(excinfo.value), f'Failed for usecase: {usecase}'

    @data(
        ('000.000.000', PaymentStatus.SUCCESS),
        ('000.200.100', PaymentStatus.PENDING),
        ('800.400.500', PaymentStatus.FAILURE),
        ('000.400.010', PaymentStatus.FAILURE),
        ('999.999.999', PaymentStatus.FAILURE),
    )
    @unpack
    def test_verify_webhook_callback_status_variants(self, result_code, expected_status):
        """It should correctly classify all payment status variants."""
        webhook_data = {'id': 'payment_1', 'result': {'code': result_code}}
        status = self.client.verify_webhook_callback_status(webhook_data)
        assert status == expected_status

    def test_verify_webhook_callback_status_missing_fields(self):
        """It should raise HyperPayBadGatewayResponse for missing fields."""
        bad_data = {'result': {}}
        with pytest.raises(HyperPayBadGatewayResponse):
            self.client.verify_webhook_callback_status(bad_data)
