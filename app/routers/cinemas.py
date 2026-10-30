from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City
from ..schemas import CinemaCreate, CinemaOut, CinemaUpdate

router = APIRouter(prefix="/api/cinemas", tags=["cinemas"])


def _get_cinema_or_404(cinema_id: int, db: Session) -> Cinema:
    cinema = db.get(Cinema, cinema_id)
    if cinema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cinema not found")
    return cinema


def _get_city_or_404(city_id: int, db: Session) -> City:
    city = db.get(City, city_id)
    if city is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="City not found")
    return city


def _to_response(cinema: Cinema) -> CinemaOut:
    return CinemaOut(
        id=cinema.id,
        name=cinema.name,
        address=cinema.address,
        cityId=cinema.city_id,
    )


@router.get("", response_model=list[CinemaOut])
def list_cinemas(db: Session = Depends(get_db)):
    cinemas = db.scalars(select(Cinema).order_by(Cinema.id))
    return [_to_response(cinema) for cinema in cinemas]


@router.get("/{cinema_id}", response_model=CinemaOut)
def get_cinema(cinema_id: int, db: Session = Depends(get_db)):
    return _to_response(_get_cinema_or_404(cinema_id, db))


@router.post("", response_model=CinemaOut, status_code=status.HTTP_201_CREATED)
def create_cinema(payload: CinemaCreate, db: Session = Depends(get_db)):
    _get_city_or_404(payload.city_id, db)
    cinema = Cinema(name=payload.name, address=payload.address, city_id=payload.city_id)
    db.add(cinema)
    db.commit()
    db.refresh(cinema)
    return _to_response(cinema)


@router.put("/{cinema_id}", response_model=CinemaOut)
@router.patch("/{cinema_id}", response_model=CinemaOut)
def update_cinema(cinema_id: int, payload: CinemaUpdate, db: Session = Depends(get_db)):
    cinema = _get_cinema_or_404(cinema_id, db)
    _get_city_or_404(payload.city_id, db)
    cinema.name = payload.name
    cinema.address = payload.address
    cinema.city_id = payload.city_id
    db.commit()
    db.refresh(cinema)
    return _to_response(cinema)


@router.delete("/{cinema_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cinema(cinema_id: int, db: Session = Depends(get_db)):
    cinema = _get_cinema_or_404(cinema_id, db)
    db.delete(cinema)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
