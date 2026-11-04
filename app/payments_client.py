"""HTTP client for the payments service that owns card-free payment records.

The service is called with the shared service token and an idempotency key, so a
retry after a timeout can never charge a booking twice.
"""

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import httpx
from fastapi import Request, status

logger = logging.getLogger(__name__)

DEFAULT_PAYMENTS_SERVICE_URL = "http://localhost:8001"
DEFAULT_PAYMENTS_SERVICE_TOKEN = "dev-service-token"
DEFAULT_TIMEOUT_SECONDS = 5.0
ATTEMPTS = 2  # the first try plus exactly one retry with the same idempotency key
DECLINE_DETAIL = "Payment declined"


@dataclass(frozen=True)
class PaymentsSettings:
    """Where the payments service lives and how long the server waits for it."""

    base_url: str
    service_token: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "PaymentsSettings":
        raw_timeout = (os.getenv("PAYMENTS_TIMEOUT_SECONDS") or "").strip()
        timeout = DEFAULT_TIMEOUT_SECONDS
        if raw_timeout:
            try:
                parsed = float(raw_timeout)
            except ValueError:
                parsed = DEFAULT_TIMEOUT_SECONDS
            if parsed > 0:
                timeout = parsed
        return cls(
            base_url=(
                os.getenv("PAYMENTS_SERVICE_URL") or DEFAULT_PAYMENTS_SERVICE_URL
            ).rstrip("/"),
            service_token=(
                os.getenv("PAYMENTS_SERVICE_TOKEN") or DEFAULT_PAYMENTS_SERVICE_TOKEN
            ).strip(),
            timeout_seconds=timeout,
        )


class PaymentsUnavailableError(Exception):
    """The payments service could not be reached or answered with an error."""


class PaymentDeclinedError(Exception):
    """The payments service declined the charge with a 402."""


class PaymentsClient:
    """Posts a booking's charge to `POST /internal/payments`."""

    def __init__(self, settings: PaymentsSettings) -> None:
        self.settings = settings

    def create_payment(
        self,
        *,
        booking_id: int,
        amount: Decimal,
        currency: str = "INR",
        idempotency_key: str,
    ) -> dict[str, object]:
        """Charge a booking; a declined payment raises, everything else answers the body."""
        url = f"{self.settings.base_url}/internal/payments"
        payload: dict[str, object] = {
            "bookingId": booking_id,
            "amount": float(amount),
            "currency": currency,
        }
        headers = {
            "X-Service-Token": self.settings.service_token,
            "Idempotency-Key": idempotency_key,
        }

        last_error = "payments service did not answer"
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.settings.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "payments service transport error on attempt %s: %s", attempt, exc
                )
                last_error = str(exc)
                continue

            if response.status_code == status.HTTP_402_PAYMENT_REQUIRED:
                raise PaymentDeclinedError(DECLINE_DETAIL)

            if response.status_code >= 500:
                logger.warning(
                    "payments service answered %s on attempt %s",
                    response.status_code,
                    attempt,
                )
                last_error = f"payments service answered {response.status_code}"
                continue

            if not 200 <= response.status_code < 300:
                raise PaymentsUnavailableError(
                    f"payments service answered {response.status_code}"
                )

            try:
                return response.json()
            except ValueError:
                raise PaymentsUnavailableError(
                    "payments service answered with a non-JSON body"
                ) from None

        raise PaymentsUnavailableError(last_error)


def get_payments_client(request: Request) -> PaymentsClient:
    """Build a client from the settings `create_app` put on app.state."""
    return PaymentsClient(request.app.state.payments_settings)
