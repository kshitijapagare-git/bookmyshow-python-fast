import math
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Guest
from ..schemas import (
    ID_PROOF_TYPES,
    GuestCreate,
    GuestOut,
    GuestPageOut,
    GuestUpdate,
)

router = APIRouter(prefix="/api/v1/guests", tags=["guests"])

DEFAULT_PAGE_SIZE = 20
GUEST_NOT_FOUND_DETAIL = "Guest not found"
EMAIL_TAKEN_DETAIL = "A guest with this email already exists"
ID_PROOF_TYPE_DETAIL = "idProofType must be one of AADHAAR, PASSPORT, DRIVING_LICENSE"
ID_PROOF_NUMBER_DETAIL = "idProofNumber must be 6-20 alphanumeric characters"
ID_PROOF_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9]{6,20}$")


def _get_guest_or_404(guest_id: int, db: Session) -> Guest:
    guest = db.get(Guest, guest_id)
    if guest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=GUEST_NOT_FOUND_DETAIL
        )
    return guest


def _guest_out(guest: Guest) -> GuestOut:
    return GuestOut(
        id=guest.id,
        firstName=guest.first_name,
        lastName=guest.last_name,
        email=guest.email,
        phone=guest.phone,
        idProofType=guest.id_proof_type,
        idProofNumber=guest.id_proof_number,
        createdAt=guest.created_at,
        updatedAt=guest.updated_at,
    )


def _ensure_email_available(
    email: str, db: Session, exclude_guest_id: int | None = None
) -> None:
    """Refuse an email another guest already owns; the schema lowercased it."""
    statement = select(Guest.id).where(Guest.email == email)
    if exclude_guest_id is not None:
        statement = statement.where(Guest.id != exclude_guest_id)
    if db.scalar(statement) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN_DETAIL
        )


def _validated_id_proof(payload: GuestCreate | GuestUpdate) -> tuple[str, str]:
    """The ticket's 400s for the id proof, which Pydantic would answer with a 422."""
    id_proof_type = payload.idProofType
    if id_proof_type is None or id_proof_type not in ID_PROOF_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ID_PROOF_TYPE_DETAIL
        )

    id_proof_number = payload.idProofNumber
    if (
        id_proof_number is None
        or ID_PROOF_NUMBER_PATTERN.fullmatch(id_proof_number) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ID_PROOF_NUMBER_DETAIL
        )
    return id_proof_type, id_proof_number


@router.post("", response_model=GuestOut, status_code=status.HTTP_201_CREATED)
def create_guest(payload: GuestCreate, db: Session = Depends(get_db)) -> GuestOut:
    id_proof_type, id_proof_number = _validated_id_proof(payload)
    _ensure_email_available(payload.email, db)
    guest = Guest(
        first_name=payload.firstName,
        last_name=payload.lastName,
        email=payload.email,
        phone=payload.phone,
        id_proof_type=id_proof_type,
        id_proof_number=id_proof_number,
    )
    db.add(guest)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN_DETAIL
        ) from None
    db.refresh(guest)
    return _guest_out(guest)


@router.get("/{guest_id}", response_model=GuestOut)
def get_guest(guest_id: int, db: Session = Depends(get_db)) -> GuestOut:
    return _guest_out(_get_guest_or_404(guest_id, db))


@router.get("", response_model=GuestPageOut)
def list_guests(
    page: int = Query(0, ge=0),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1),
    db: Session = Depends(get_db),
) -> GuestPageOut:
    """One page of the guest directory, ordered by id so pages stay stable."""
    guests = db.scalars(
        select(Guest).order_by(Guest.id).offset(page * size).limit(size)
    )
    total = db.scalar(select(func.count(Guest.id))) or 0
    return GuestPageOut(
        content=[_guest_out(guest) for guest in guests],
        page=page,
        size=size,
        totalElements=total,
        totalPages=math.ceil(total / size) if total else 0,
    )


@router.put("/{guest_id}", response_model=GuestOut)
def update_guest(
    guest_id: int, payload: GuestUpdate, db: Session = Depends(get_db)
) -> GuestOut:
    guest = _get_guest_or_404(guest_id, db)
    id_proof_type, id_proof_number = _validated_id_proof(payload)
    _ensure_email_available(payload.email, db, exclude_guest_id=guest.id)
    guest.first_name = payload.firstName
    guest.last_name = payload.lastName
    guest.email = payload.email
    guest.phone = payload.phone
    guest.id_proof_type = id_proof_type
    guest.id_proof_number = id_proof_number
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN_DETAIL
        ) from None
    db.refresh(guest)
    return _guest_out(guest)


@router.delete("/{guest_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_guest(guest_id: int, db: Session = Depends(get_db)) -> Response:
    guest = _get_guest_or_404(guest_id, db)
    db.delete(guest)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
