"""Tests for the HyperPayClient class and related payment flows."""

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
            access_token='test-token',
            base_url='https://fake.hyperpay.com',
            entity_id='abcd1234',
            slug='hyperpay',
            test_mode='EXTERNAL'
        )

    @patch('requests.post')
    def test_create_checkout_success(self, mock_post):
        """It should successfully create a checkout."""
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

    def test_create_checkout_payload_with_test_mode(self):
        payload = {"amount": "100.00", "currency": "USD"}

        with patch("hyperpay.client.requests.post") as mock_post:
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {
                'id': 'chk_123',
                'ndc': 'nonce_123',
                'integrity': 'int_123',
                'result': {'code': '000.200.100'},
            }
            mock_post.return_value.status_code = 200
            mock_post.return_value = mock_response
            self.client.create_checkout(payload)

            # Assert that the payload sent contains the 3DS parameters
            called_payload = mock_post.call_args[0][1]
            assert called_payload["customParameters[3DS2_enrolled]"] == "true"
            assert called_payload["customParameters[3DS2_flow]"] == "challenge"
            assert called_payload["testMode"] == "EXTERNAL"

            payload = {"amount": "100.00", "currency": "USD"}
            self.client.test_mode = None
            self.client.create_checkout(payload)
            # Assert that the payload sent does not contain 3DS parameters
            called_payload = mock_post.call_args[0][1]
            assert 'customParameters[3DS2_enrolled]' not in called_payload
            assert 'customParameters[3DS2_flow]' not in called_payload
            assert 'testMode' not in called_payload

    @patch('requests.post')
    def test_create_checkout_invalid_response(self, mock_post):
        """It should raise HyperPayException for invalid response format."""
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {'unexpected': 'format'}
        mock_post.return_value = mock_response

        with pytest.raises(HyperPayException):
            self.client.create_checkout({'amount': '10.00'})

    @patch('requests.post')
    def test_create_checkout_http_error(self, mock_post):
        """Should raise HyperPayException if raise_for_status() fails."""
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
    def test_get_checkout_status_success_creates_auditlog(self, mock_get):
        """It should verify checkout and create an AuditLog."""
        assert not AuditLog.objects.filter(gateway='hyperpay').exists()

        mock_response = Mock()
        mock_response.json.return_value = {'result': {'code': '000.000.000'}}
        mock_get.return_value = mock_response

        resp_data = self.client.get_checkout_status({'checkout_id': 'chk_123'})
        assert resp_data['result']['code'] == '000.000.000'

        assert AuditLog.objects.filter(
            gateway='hyperpay',
            action='received_gateway_response',
        ).exists()

    @patch('requests.get')
    def test_get_checkout_status_failure(self, mock_get):
        """It should raise exception for failed status."""
        mock_response = Mock()
        mock_response.json.return_value = {'result': {'code': '999.999.999'}}
        mock_get.return_value = mock_response

        with pytest.raises(HyperPayException):
            self.client.get_checkout_status({'checkout_id': 'chk_123'})

    @data(
        ('HTTP error', Exception('500 Server Error'), '500 Server Error'),
        ('Invalid JSON', ValueError('No JSON object could be decoded'), 'No JSON object could be decoded'),
        ('Generic exception', RuntimeError('Something broke'), 'Something broke'),
    )
    @unpack
    @patch("requests.get")
    def test_get_checkout_status_exceptions(self, usecase, exception_obj, expected_msg, mock_get):
        """Should raise HyperPayException for different error cases."""
        mock_response = Mock()
        mock_response.json.side_effect = exception_obj
        mock_get.return_value = mock_response

        with pytest.raises(HyperPayException) as excinfo:
            self.client.get_checkout_status({'checkout_id': 'test123'})

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
    def test_verify_status_variants(self, result_code, expected_status):
        """It should correctly classify all payment status variants."""
        webhook_data = {'id': 'payment_1', 'result': {'code': result_code}}
        status = self.client.verify_status(webhook_data)
        assert status == expected_status

    def test_verify_status_missing_fields(self):
        """It should raise HyperPayBadGatewayResponse for missing fields."""
        bad_data = {'result': {}}
        with pytest.raises(HyperPayBadGatewayResponse):
            self.client.verify_status(bad_data)
