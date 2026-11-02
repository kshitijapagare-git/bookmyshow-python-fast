import math
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import OtpChallenge, OtpRequest, User
from ..schemas import OtpRequestIn, OtpRequestOut, OtpVerifyIn, OtpVerifyOut, UserOut
from ..security import (
    JWT_TTL_SECONDS,
    generate_code,
    get_current_user,
    hash_code,
    issue_token,
    verify_code,
)
from ..sms import SmsSender, get_sms_sender

router = APIRouter(prefix="/api/auth", tags=["auth"])
me_router = APIRouter(prefix="/api", tags=["auth"])

MAX_ATTEMPTS = 5
OTP_TTL_SECONDS = 300
RESEND_AFTER_SECONDS = 30
RATE_LIMIT_MAX_REQUESTS = 3
RATE_LIMIT_WINDOW_SECONDS = 600

SUPPORTED_COUNTRY_CODE = "+91"
COUNTRY_CODE_DETAIL = "countryCode must be +91"
WRONG_CODE_DETAIL = "That code is not right"
EXPIRED_DETAIL = "This code has expired. Request a new one."
LOCKED_DETAIL = "Too many wrong attempts. Request a new code."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _problem_detail(
    request: Request,
    status_code: int,
    detail: str,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    content: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
    }
    content.update(extra)
    return JSONResponse(status_code=status_code, headers=headers, content=content)


def _recent_requests(phone: str, db: Session) -> list[datetime]:
    window_start = _now() - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    rows = db.scalars(
        select(OtpRequest.requested_at)
        .where(OtpRequest.phone == phone, OtpRequest.requested_at > window_start)
        .order_by(OtpRequest.requested_at, OtpRequest.id)
    )
    return [_as_utc(moment) for moment in rows]


@router.post("/otp/request", status_code=status.HTTP_202_ACCEPTED)
def request_otp(
    payload: OtpRequestIn,
    request: Request,
    db: Session = Depends(get_db),
    sms: SmsSender = Depends(get_sms_sender),
) -> JSONResponse:
    if payload.countryCode.strip() != SUPPORTED_COUNTRY_CODE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=COUNTRY_CODE_DETAIL
        )

    phone = f"{SUPPORTED_COUNTRY_CODE}{payload.phone}"
    recent = _recent_requests(phone, db)
    if len(recent) >= RATE_LIMIT_MAX_REQUESTS:
        window_end = recent[0] + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        retry_after = max(1, math.ceil((window_end - _now()).total_seconds()))
        minutes = max(1, math.ceil(retry_after / 60))
        return _problem_detail(
            request,
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many codes requested. Try again in {minutes} minutes.",
            headers={"Retry-After": str(retry_after)},
            retryAfterSeconds=retry_after,
        )

    now = _now()
    code = generate_code()
    challenge = OtpChallenge(
        id=str(uuid.uuid4()),
        phone=phone,
        code_hash=hash_code(code=code),
        expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
        attempts=0,
        created_at=now,
    )
    db.execute(delete(OtpChallenge).where(OtpChallenge.phone == phone))
    db.add(challenge)
    db.add(OtpRequest(phone=phone, requested_at=now))
    db.commit()

    sms.send(phone=phone, code=code)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=OtpRequestOut(
            challengeId=challenge.id,
            resendAfterSeconds=RESEND_AFTER_SECONDS,
            expiresInSeconds=OTP_TTL_SECONDS,
        ).model_dump(),
    )


@router.post("/otp/verify", response_model=OtpVerifyOut)
def verify_otp(
    payload: OtpVerifyIn,
    request: Request,
    db: Session = Depends(get_db),
) -> OtpVerifyOut | JSONResponse:
    challenge = db.get(OtpChallenge, payload.challengeId)
    if challenge is None:
        return _problem_detail(
            request,
            status.HTTP_400_BAD_REQUEST,
            WRONG_CODE_DETAIL,
            attemptsLeft=MAX_ATTEMPTS,
        )

    now = _now()
    if _as_utc(challenge.expires_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=EXPIRED_DETAIL
        )

    if challenge.attempts >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED, detail=LOCKED_DETAIL
        )

    if not verify_code(code=payload.code, code_hash=challenge.code_hash):
        challenge.attempts += 1
        db.commit()
        if challenge.attempts >= MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED, detail=LOCKED_DETAIL
            )
        return _problem_detail(
            request,
            status.HTTP_400_BAD_REQUEST,
            WRONG_CODE_DETAIL,
            attemptsLeft=MAX_ATTEMPTS - challenge.attempts,
        )

    user = db.scalar(select(User).where(User.phone == challenge.phone))
    if user is None:
        user = User(phone=challenge.phone, name=None, created_at=now)
        db.add(user)
        db.flush()

    user_id = user.id
    user_phone = user.phone
    user_name = user.name

    db.delete(challenge)
    db.commit()

    return OtpVerifyOut(
        token=issue_token(user_id=user_id),
        expiresInSeconds=JWT_TTL_SECONDS,
        user=UserOut(id=user_id, phone=user_phone, name=user_name),
    )


@me_router.get("/me", response_model=UserOut)
def get_me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, phone=user.phone, name=user.name)
