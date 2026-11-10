"""HTTP client for the reviews service that owns ratings and review text.

The service is called with the shared service token, never with the moviegoer's
JWT, so the booking service stays the only place a review's verified flag is
decided -- by checking a paid booking before the review is sent on.
"""

import logging
import os
from dataclasses import dataclass

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

DEFAULT_REVIEWS_SERVICE_URL = "http://localhost:8002"
DEFAULT_REVIEWS_SERVICE_TOKEN = "dev-service-token"
DEFAULT_TIMEOUT_SECONDS = 5.0
ATTEMPTS = 2  # the first try plus exactly one retry; the reads and the upsert are safe


@dataclass(frozen=True)
class ReviewsSettings:
    """Where the reviews service lives and how long the server waits for it."""

    base_url: str
    service_token: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "ReviewsSettings":
        raw_timeout = (os.getenv("REVIEWS_TIMEOUT_SECONDS") or "").strip()
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
                os.getenv("REVIEWS_SERVICE_URL") or DEFAULT_REVIEWS_SERVICE_URL
            ).rstrip("/"),
            service_token=(
                os.getenv("REVIEWS_SERVICE_TOKEN") or DEFAULT_REVIEWS_SERVICE_TOKEN
            ).strip(),
            timeout_seconds=timeout,
        )


class ReviewsUnavailableError(Exception):
    """The reviews service could not be reached or answered with an error."""


def _body(response: httpx.Response) -> object:
    """The decoded JSON body, or the typed error when it is not JSON at all."""
    try:
        return response.json()
    except ValueError:
        raise ReviewsUnavailableError(
            "reviews service answered with a non-JSON body"
        ) from None


def _as_object(body: object) -> dict[str, object]:
    """A single JSON object, the shape both endpoints answer with."""
    if not isinstance(body, dict):
        raise ReviewsUnavailableError(
            "reviews service answered with something other than an object"
        )
    return body


class ReviewsClient:
    """Reads a movie's reviews and submits one, through the internal ratings API."""

    def __init__(self, settings: ReviewsSettings) -> None:
        self.settings = settings

    def list_reviews(
        self, *, movie_id: int, page: int, page_size: int
    ) -> dict[str, object]:
        """One page of a movie's reviews plus its summary, as the service answered it."""
        url = (
            f"{self.settings.base_url}/internal/reviews"
            f"?movieId={movie_id}&page={page}&pageSize={page_size}"
        )
        headers = {"X-Service-Token": self.settings.service_token}

        last_error = "reviews service did not answer"
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = httpx.get(
                    url, headers=headers, timeout=self.settings.timeout_seconds
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "reviews service transport error on attempt %s: %s", attempt, exc
                )
                last_error = str(exc)
                continue

            if response.status_code >= 500:
                logger.warning(
                    "reviews service answered %s on attempt %s",
                    response.status_code,
                    attempt,
                )
                last_error = f"reviews service answered {response.status_code}"
                continue

            if not 200 <= response.status_code < 300:
                raise ReviewsUnavailableError(
                    f"reviews service answered {response.status_code}"
                )

            return _as_object(_body(response))

        raise ReviewsUnavailableError(last_error)

    def list_booking_reviews(
        self, *, movie_id: int, booking_id: int
    ) -> list[dict[str, object]]:
        """Every review already stored for one booking, transferred ones included."""
        url = (
            f"{self.settings.base_url}/internal/reviews"
            f"?movieId={movie_id}&bookingId={booking_id}"
        )
        headers = {"X-Service-Token": self.settings.service_token}

        try:
            response = httpx.get(
                url, headers=headers, timeout=self.settings.timeout_seconds
            )
        except httpx.HTTPError as exc:
            logger.warning("reviews service booking lookup error: %s", exc)
            raise ReviewsUnavailableError(str(exc)) from None

        if not 200 <= response.status_code < 300:
            raise ReviewsUnavailableError(
                f"reviews service answered {response.status_code}"
            )

        rows = _as_object(_body(response)).get("reviews")
        if not isinstance(rows, list):
            raise ReviewsUnavailableError(
                "reviews service answered without a reviews list"
            )
        reviews: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ReviewsUnavailableError(
                    "reviews service answered with a review that is not an object"
                )
            reviews.append(row)
        return reviews

    def transfer_review(self, *, review_id: int) -> dict[str, object]:
        """Mark a review TRANSFERRED; an unknown review simply has nothing to mark."""
        url = f"{self.settings.base_url}/internal/reviews/{review_id}/transfer"
        headers = {"X-Service-Token": self.settings.service_token}

        try:
            response = httpx.post(
                url, headers=headers, timeout=self.settings.timeout_seconds
            )
        except httpx.HTTPError as exc:
            logger.warning("reviews service transfer error: %s", exc)
            raise ReviewsUnavailableError(str(exc)) from None

        if response.status_code == 404:
            return {}

        if not 200 <= response.status_code < 300:
            raise ReviewsUnavailableError(
                f"reviews service answered {response.status_code}"
            )

        return _as_object(_body(response))

    def create_review(
        self,
        *,
        movie_id: int,
        user_id: int,
        booking_id: int | None,
        group_id: int | None = None,
        rating: int,
        comment: str,
        verified: bool,
    ) -> tuple[dict[str, object], bool]:
        """Submit a review; the flag is True when the service stored a brand new one."""
        url = f"{self.settings.base_url}/internal/reviews"
        payload: dict[str, object] = {
            "movieId": movie_id,
            "userId": user_id,
            "bookingId": booking_id,
            "rating": rating,
            "comment": comment,
            "verified": verified,
        }
        # A booking review sends exactly what it sent before: groupId only when set.
        if group_id is not None:
            payload["groupId"] = group_id
        headers = {"X-Service-Token": self.settings.service_token}

        last_error = "reviews service did not answer"
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
                    "reviews service submit transport error on attempt %s: %s",
                    attempt,
                    exc,
                )
                last_error = str(exc)
                continue

            if response.status_code >= 500:
                logger.warning(
                    "reviews service answered %s to the submit on attempt %s",
                    response.status_code,
                    attempt,
                )
                last_error = f"reviews service answered {response.status_code}"
                continue

            if not 200 <= response.status_code < 300:
                raise ReviewsUnavailableError(
                    f"reviews service answered {response.status_code}"
                )

            return _as_object(_body(response)), response.status_code == 201

        raise ReviewsUnavailableError(last_error)


def get_reviews_client(request: Request) -> ReviewsClient:
    """Build a client from the settings `create_app` put on app.state."""
    return ReviewsClient(request.app.state.reviews_settings)
