# zeitlabs_payments/providers/hyperpay/client.py

import base64
import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class HyperPayClient:
    """
    Handles API communication with Nelc-HyperPay.
    """

    def __init__(self, client_id: str, client_secret: str, base_url: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self.access_token: Optional[str] = None

    def _generate_access_token(self) -> None:
        """Generate an access token from HyperPay OAuth API."""
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials", "scope": "pay"}

        response = requests.post(f"{self.base_url}/oauth2/v1/token", headers=headers, data=data)
        if response.status_code == 200:
            self.access_token = response.json().get("access_token")
        else:
            logger.error("HyperPay token API failed: %s", response.text)
            raise Exception("Something went wrong with Token API")

    def _ensure_token(self) -> None:
        """Ensure we have a valid token before making API calls."""
        if not self.access_token:
            self._generate_access_token()

    def create_checkout(self, payload: dict) -> Dict[str, str]:
        """Create a new checkout session in HyperPay."""
        self._ensure_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        response = requests.post(
            f"{self.base_url}/payment-gateway/v1/checkout", headers=headers, json=payload
        )
        if response.status_code == 200:
            return {
                "checkout_id": response.json().get("id"),
                "nonce_id": response.json().get("ndc"),
                "integrity": response.json().get("integrity"),
            }
        logger.error("HyperPay checkout API failed: %s", response.text)
        raise Exception("Unable to create checkout.")

    def verify_checkout_status(self, params: dict) -> dict:
        """Verify checkout session status from HyperPay."""
        self._ensure_token()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        response = requests.get(
            f"{self.base_url}/payment-gateway/v1/checkout/status",
            headers=headers,
            params=params,
        )
        if response.status_code == 200:
            return response.json()
        logger.error("HyperPay status API failed: %s", response.text)
        raise Exception("Unable to verify checkout status.")
