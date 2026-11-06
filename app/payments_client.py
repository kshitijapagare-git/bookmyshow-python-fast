import json
import os
import urllib.error
import urllib.request


class PaymentsUnavailable(Exception):
    """Raised when the payments service cannot settle or release a transfer."""


def _base_url() -> str:
    return os.getenv("PAYMENTS_BASE_URL", "http://localhost:8001")


def _service_token() -> str:
    return os.getenv("PAYMENTS_SERVICE_TOKEN", "dev-service-token")


def _post(path: str, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(
        url=f"{_base_url()}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Service-Token": _service_token(),
            **headers,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw_body = response.read().decode()
            if not 200 <= response.status < 300:
                raise PaymentsUnavailable(f"Payments service answered {response.status}")
    except urllib.error.HTTPError as exc:
        raise PaymentsUnavailable(f"Payments service answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise PaymentsUnavailable(str(exc.reason)) from exc
    except OSError as exc:
        raise PaymentsUnavailable(str(exc)) from exc

    try:
        return json.loads(raw_body)
    except ValueError as exc:
        raise PaymentsUnavailable("Payments service answered with invalid JSON") from exc


def settle_transfer(
    *,
    booking_id: int,
    from_user_id: int,
    to_user_id: int,
    amount: float,
    fee: float,
) -> dict[str, object]:
    return _post(
        "/internal/transfers",
        {
            "bookingId": booking_id,
            "fromUserId": from_user_id,
            "toUserId": to_user_id,
            "amount": amount,
            "fee": fee,
        },
        {"Idempotency-Key": f"transfer:{booking_id}"},
    )


def release_transfer(*, booking_id: int) -> dict[str, object]:
    return _post("/internal/transfers/release", {"bookingId": booking_id}, {})
