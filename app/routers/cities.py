from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City
from ..schemas import CityCreate, CityOut, CityUpdate

router = APIRouter(prefix="/api/cities", tags=["cities"])


def _get_city_or_404(city_id: int, db: Session) -> City:
    city = db.get(City, city_id)
    if city is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="City not found")
    return city


@router.get("", response_model=list[CityOut])
def list_cities(db: Session = Depends(get_db)):
    return list(db.scalars(select(City).order_by(City.id)))


@router.get("/{city_id}", response_model=CityOut)
def get_city(city_id: int, db: Session = Depends(get_db)):
    return _get_city_or_404(city_id, db)


@router.post("", response_model=CityOut, status_code=status.HTTP_201_CREATED)
def create_city(payload: CityCreate, db: Session = Depends(get_db)):
    city = City(name=payload.name, state=payload.state)
    db.add(city)
    db.commit()
    db.refresh(city)
    return city


@router.put("/{city_id}", response_model=CityOut)
@router.patch("/{city_id}", response_model=CityOut)
def update_city(city_id: int, payload: CityUpdate, db: Session = Depends(get_db)):
    city = _get_city_or_404(city_id, db)
    city.name = payload.name
    city.state = payload.state
    db.commit()
    db.refresh(city)
    return city


@router.delete("/{city_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_city(city_id: int, db: Session = Depends(get_db)):
    city = _get_city_or_404(city_id, db)

    has_cinemas = db.scalar(select(Cinema.id).where(Cinema.city_id == city_id).limit(1))
    if has_cinemas is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="City is referenced by a cinema",
        )

    db.delete(city)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
