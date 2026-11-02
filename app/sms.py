import logging
from typing import Protocol

logger = logging.getLogger(__name__)

PHONE_MASK_KEEP = 2


class SmsSender(Protocol):
    def send(self, *, phone: str, code: str) -> None: ...


def mask_phone(phone: str) -> str:
    """Keep the country code, the first two and the last two digits, mask the rest."""
    digits = phone.strip().lstrip("+")
    country_code = ""
    if digits.startswith("91") and len(digits) == 12:
        country_code = "+91"
        digits = digits[2:]
    if len(digits) <= PHONE_MASK_KEEP * 2:
        return f"{country_code}{'X' * len(digits)}"
    head = digits[:PHONE_MASK_KEEP]
    tail = digits[-PHONE_MASK_KEEP:]
    masked = "X" * (len(digits) - PHONE_MASK_KEEP * 2)
    return f"{country_code}{head}{masked}{tail}"


class LoggingSmsSender:
    """Dev-only stub: logs the masked destination, never the code."""

    def send(self, *, phone: str, code: str) -> None:
        logger.info("Sending OTP to %s", mask_phone(phone))


def get_sms_sender() -> SmsSender:
    return LoggingSmsSender()
