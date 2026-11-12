from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .kafka_producer import KafkaUnavailableError
from .seating import SeatsUnavailableError


def problem_detail_content(
    status_code: int, detail: str, instance: str
) -> dict[str, object]:
    """The RFC 7807 body every error response in the API shares."""
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    return {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
    }


def _problem_detail_response(request: Request, status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=problem_detail_content(status_code, detail, request.url.path),
    )


async def problem_details_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    return _problem_detail_response(request, exc.status_code, str(exc.detail))


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}"
        for error in exc.errors()
    )
    return _problem_detail_response(request, 422, detail or "Invalid request")


async def seats_unavailable_exception_handler(
    request: Request, exc: SeatsUnavailableError
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "type": "about:blank",
            "title": "Conflict",
            "status": 409,
            "detail": exc.detail,
            "seatIds": exc.seat_ids,
            "instance": request.url.path,
        },
    )


async def kafka_unavailable_exception_handler(
    request: Request, exc: KafkaUnavailableError
) -> JSONResponse:
    return _problem_detail_response(request, 503, exc.detail)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, problem_details_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(
        SeatsUnavailableError, seats_unavailable_exception_handler
    )
    app.add_exception_handler(KafkaUnavailableError, kafka_unavailable_exception_handler)
