"""HyperPay provider exceptions."""
from zeitlabs_payments.exceptions import GatewayError


class HyperPayException(GatewayError):
    """HyperPay exception."""


class HyperPayBadGatewayResponse(HyperPayException):
    """HyperPay bad response exception."""
