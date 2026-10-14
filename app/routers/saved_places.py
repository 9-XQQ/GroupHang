"""Private saved places and explicit, versioned imports into member trips."""
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import SavedPlace, TripDestination, TripPlaceImport, User
from ..schemas import SavedPlaceCreate, SavedPlaceUpdate, PlaceBatchImport
from ..security import get_current_user
from ..services.access import require_trip_member, lock_trip_active
from ..services.ws import manager

router = APIRouter(tags=["saved-places"])


def place_key(place: dict) -> str:
    raw = ("poi:" + place["poi_id"]) if place.get("poi_id") else (
        f"{place['name'].strip().lower()}:{place['lat']:.6f},{place['lng']:.6f}")
    return hashlib.sha256(raw.encode()).hexdigest()


def saved_dict(place: SavedPlace) -> dict:
    return {field: getattr(place, field) for field in (
        "id", "name", "city", "address", "lat", "lng", "category", "poi_id", "tags",
        "private_note", "expected_stay_min", "revision", "created_at", "updated_at")}


def planning_place(place: dict) -> dict:
    """Never copy personal notes/tags into a shared destination."""
    return {field: place.get(field) for field in (
        "name", "address", "lat", "lng", "category", "expected_stay_min")}


@router.get("/users/me/saved-places")
async def list_saved_places(response: Response, tag: str = Query(default="", max_length=30),
                            city: str = Query(default="", max_length=30),
                            offset: int = Query(default=0, ge=0, le=10000),
                            limit: int = Query(default=30, ge=1, le=100),
                            user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    filters = [SavedPlace.user_id == user.id]
    if tag.strip():
        filters.append(SavedPlace.tags.contains([tag.strip()]))
    if city.strip():
        filters.append(SavedPlace.city == city.strip())
    total = await db.scalar(select(func.count()).select_from(SavedPlace).where(*filters))
    places = (await db.execute(select(SavedPlace).where(*filters).order_by(
        SavedPlace.updated_at.desc(), SavedPlace.id.desc()).offset(offset).limit(limit))).scalars().all()
    return {"places": [saved_dict(p) for p in places], "total": total, "offset": offset, "limit": limit}


@router.post("/users/me/saved-places", status_code=201)
async def save_place(body: SavedPlaceCreate, response: Response, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict:
    values = body.model_dump()
    key = place_key(values)
    stmt = insert(SavedPlace).values(user_id=user.id, place_key=key, **values).on_conflict_do_nothing(
        constraint="uq_saved_place_owner_key").returning(SavedPlace.id)
    created_id = await db.scalar(stmt)
    place = (await db.execute(select(SavedPlace).where(
        SavedPlace.user_id == user.id, SavedPlace.place_key == key))).scalar_one()
    await db.commit()
    if created_id is None:
        response.status_code = 200  # Existing memories are never overwritten on duplicate saves.
    response.headers["Cache-Control"] = "no-store"
    return saved_dict(place)


@router.put("/users/me/saved-places/{place_id}")
async def edit_saved_place(place_id: int, body: SavedPlaceUpdate, response: Response,
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    place = (await db.execute(select(SavedPlace).where(
        SavedPlace.id == place_id, SavedPlace.user_id == user.id).with_for_update())).scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "收藏不存在")
    if place.revision != body.revision:
        raise HTTPException(409, "收藏已在其他页面修改，请刷新")
    for key, value in body.model_dump(exclude={"revision"}).items():
        setattr(place, key, value)
    place.revision += 1
    await db.commit()
    await db.refresh(place)
    response.headers["Cache-Control"] = "no-store"
    return saved_dict(place)


@router.delete("/users/me/saved-places/{place_id}", status_code=204)
async def remove_saved_place(place_id: int, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    place = (await db.execute(select(SavedPlace).where(
        SavedPlace.id == place_id, SavedPlace.user_id == user.id).with_for_update())).scalar_one_or_none()
    if place is None:
        raise HTTPException(404, "收藏不存在")
    await db.delete(place)
    await db.commit()
    return Response(status_code=204)


@router.post("/trips/{trip_id}/places/import")
async def import_places(trip_id: int, body: PlaceBatchImport, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)) -> dict:
    participant = await require_trip_member(db, trip_id, user)
    if body.confirm_for_planning and participant.role != "creator":
        raise HTTPException(403, "只有发起人可以确认地点并保存规划参数")
    trip = await lock_trip_active(db, trip_id)
    if body.confirm_for_planning and trip.primary_workflow != "itinerary":
        raise HTTPException(409, "请先由发起人切换为多地点路线主方案")
    selections = []
    for selection in body.places:
        key = ("saved:" + str(selection.saved_place_id)) if selection.saved_place_id else (
            "place:" + place_key(selection.place.model_dump()))
        key = hashlib.sha256(key.encode()).hexdigest()
        existing = await db.scalar(select(TripPlaceImport.destination_id).where(
            TripPlaceImport.trip_id == trip_id, TripPlaceImport.user_id == user.id,
            TripPlaceImport.source_key == key))
        selections.append((key, selection, existing))
    if all(existing for _, _, existing in selections):
        already_prepared = not body.confirm_for_planning
        if body.confirm_for_planning:
            settings_match = all(getattr(trip, key) == value for key, value in body.planning_settings.model_dump(exclude_unset=True).items())
            selected = (await db.execute(select(TripDestination).where(
                TripDestination.id.in_([x[2] for x in selections])))).scalars().all()
            already_prepared = settings_match and all(d.visit_status == "must_visit" for d in selected)
        if already_prepared:
            return {"destination_ids": list(dict.fromkeys(x[2] for x in selections)),
                    "input_version": trip.input_version, "replayed": True,
                    "prepared_for_planning": body.confirm_for_planning}
    if trip.input_version != body.expected_version:
        raise HTTPException(409, "行程输入已变化，请刷新推荐或收藏列表后重试")
    count = await db.scalar(select(func.count()).select_from(TripDestination).where(
        TripDestination.trip_id == trip_id, TripDestination.visit_status != "excluded"))
    new_keys = {key for key, _, existing in selections if not existing}
    if count + len(new_keys) > 10:
        raise HTTPException(409, "当前行程最多保留10个参与规划的候选地点，请先移除或排除部分地点")
    ids, added = [], {}
    for key, selection, existing in selections:
        if existing or key in added:
            ids.append(existing or added[key])
            continue
        if selection.saved_place_id:
            saved = (await db.execute(select(SavedPlace).where(
                SavedPlace.id == selection.saved_place_id, SavedPlace.user_id == user.id,
            ).with_for_update())).scalar_one_or_none()
            if saved is None:
                raise HTTPException(404, "收藏不存在")
            if saved.revision != selection.revision:
                raise HTTPException(409, "收藏已变化，请刷新后重新选择")
            values = planning_place(saved_dict(saved))
        else:
            values = planning_place(selection.place.model_dump())
        destination = TripDestination(trip_id=trip_id, submitted_by=user.id, **values)
        db.add(destination)
        await db.flush()
        db.add(TripPlaceImport(trip_id=trip_id, user_id=user.id, source_key=key, destination_id=destination.id))
        added[key] = destination.id
        ids.append(destination.id)
    if body.confirm_for_planning:
        selected = (await db.execute(select(TripDestination).where(
            TripDestination.id.in_(list(dict.fromkeys(ids)))))).scalars().all()
        for destination in selected:
            destination.visit_status = "must_visit"
        for key, value in body.planning_settings.model_dump().items():
            setattr(trip, key, value)
        await db.flush()
        active_count = await db.scalar(select(func.count()).select_from(TripDestination).where(
            TripDestination.trip_id == trip_id, TripDestination.visit_status.in_(["must_visit", "optional"])))
        if not 2 <= active_count <= 10:
            raise HTTPException(409, "正式路线需要2～10个必去或可选地点；本次操作未保存")
    trip.input_version += 1
    await db.commit()
    await manager.broadcast(trip_id, {"type": "destinations_updated", "trip_id": trip_id,
                                    "input_version": trip.input_version})
    if body.confirm_for_planning:
        await manager.broadcast(trip_id, {"type": "planning_settings_updated", "trip_id": trip_id,
                                        "input_version": trip.input_version})
    return {"destination_ids": list(dict.fromkeys(ids)), "input_version": trip.input_version, "replayed": False,
            "prepared_for_planning": body.confirm_for_planning}
