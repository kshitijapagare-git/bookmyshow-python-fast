from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cinema, City
from ..schemas import CityCreate, CityOut, CityUpdate
from ..slug import slugify

router = APIRouter(prefix="/api/cities", tags=["cities"])


def _get_city_or_404(city_id: int, db: Session) -> City:
    city = db.get(City, city_id)
    if city is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="City not found")
    return city


def _ensure_slug_available(slug: str, db: Session, exclude_city_id: int | None = None) -> None:
    existing_id = db.scalar(select(City.id).where(City.slug == slug))
    if existing_id is not None and existing_id != exclude_city_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"City slug is already taken: {slug}",
        )


@router.get("", response_model=list[CityOut])
def list_cities(db: Session = Depends(get_db)):
    return list(db.scalars(select(City).order_by(City.id)))


@router.get("/{city_id}", response_model=CityOut)
def get_city(city_id: int, db: Session = Depends(get_db)):
    return _get_city_or_404(city_id, db)


@router.post("", response_model=CityOut, status_code=status.HTTP_201_CREATED)
def create_city(payload: CityCreate, db: Session = Depends(get_db)):
    slug = slugify(payload.name)
    _ensure_slug_available(slug, db)
    city = City(name=payload.name, state=payload.state, slug=slug)
    db.add(city)
    db.commit()
    db.refresh(city)
    return city


@router.put("/{city_id}", response_model=CityOut)
@router.patch("/{city_id}", response_model=CityOut)
def update_city(city_id: int, payload: CityUpdate, db: Session = Depends(get_db)):
    city = _get_city_or_404(city_id, db)
    slug = slugify(payload.name)
    _ensure_slug_available(slug, db, exclude_city_id=city_id)
    city.name = payload.name
    city.state = payload.state
    city.slug = slug
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
