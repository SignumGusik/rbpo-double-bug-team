from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.dependencies import CurrentUser, DbSession, OptionalUser
from app.models import Role, User
from app.schemas import (
    AuditRead,
    EventCreate,
    EventOrganizerRead,
    EventPublic,
    EventUpdate,
    HealthResponse,
    LoginRequest,
    ParticipantRead,
    RegistrationRead,
    TokenResponse,
    UserRead,
)
from app.security import create_access_token, hash_password, verify_password


DEMO_USERS = [
    ("organizer1@campus.local", "Organizer One", Role.ORGANIZER),
    ("organizer2@campus.local", "Organizer Two", Role.ORGANIZER),
    ("participant1@campus.local", "Participant One", Role.PARTICIPANT),
    ("participant2@campus.local", "Participant Two", Role.PARTICIPANT),
    ("participant3@campus.local", "Participant Three", Role.PARTICIPANT),
]


def seed_demo_users(db: Session) -> None:
    if db.scalar(select(User.id).limit(1)) is not None:
        return
    password = get_settings().seed_password
    db.add_all(
        User(email=email, display_name=name, password_hash=hash_password(password), role=role)
        for email, name, role in DEMO_USERS
    )
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_demo_users(db)
    yield


app = FastAPI(
    title="Campus Events",
    version="0.1.0",
    description="Event registration with limited capacity — minimal runnable foundation",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/auth/login", response_model=TokenResponse, tags=["auth"])
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(user.id, user.role))


@app.get("/users/me", response_model=UserRead, tags=["auth"])
def read_me(user: CurrentUser) -> User:
    return user


@app.get("/events", response_model=list[EventPublic], tags=["events"])
def list_events(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[EventPublic]:
    return [services.to_public(e) for e in services.list_public_events(db, limit, offset)]


@app.get("/events/{event_id}", response_model=EventPublic, tags=["events"])
def read_event(event_id: int, user: OptionalUser, db: DbSession) -> EventPublic:
    return services.to_public(services.get_visible_event(db, event_id, user))


@app.post(
    "/events",
    response_model=EventOrganizerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["organizer"],
)
def create_event(payload: EventCreate, user: CurrentUser, db: DbSession) -> EventOrganizerRead:
    return services.to_organizer(services.create_event(db, user, payload))


@app.get("/my/events", response_model=list[EventOrganizerRead], tags=["organizer"])
def my_events(user: CurrentUser, db: DbSession) -> list[EventOrganizerRead]:
    return [services.to_organizer(e) for e in services.list_my_events(db, user)]


@app.patch("/events/{event_id}", response_model=EventOrganizerRead, tags=["organizer"])
def update_event(
    event_id: int, payload: EventUpdate, user: CurrentUser, db: DbSession
) -> EventOrganizerRead:
    return services.to_organizer(services.update_event(db, user, event_id, payload))


@app.post("/events/{event_id}/publish", response_model=EventOrganizerRead, tags=["organizer"])
def publish_event(event_id: int, user: CurrentUser, db: DbSession) -> EventOrganizerRead:
    return services.to_organizer(services.publish_event(db, user, event_id))


@app.post("/events/{event_id}/cancel", response_model=EventOrganizerRead, tags=["organizer"])
def cancel_event(event_id: int, user: CurrentUser, db: DbSession) -> EventOrganizerRead:
    return services.to_organizer(services.cancel_event(db, user, event_id))


@app.get(
    "/events/{event_id}/participants",
    response_model=list[ParticipantRead],
    tags=["organizer"],
)
def list_participants(event_id: int, user: CurrentUser, db: DbSession) -> list[ParticipantRead]:
    return services.list_participants(db, user, event_id)


@app.get("/events/{event_id}/audit", response_model=list[AuditRead], tags=["organizer"])
def list_audit(event_id: int, user: CurrentUser, db: DbSession) -> list[AuditRead]:
    return [AuditRead.model_validate(entry) for entry in services.list_audit(db, user, event_id)]


@app.post(
    "/events/{event_id}/registrations",
    response_model=RegistrationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["participant"],
)
def register(event_id: int, user: CurrentUser, db: DbSession) -> RegistrationRead:
    return services.to_registration(services.register(db, user, event_id))


@app.get("/my/registrations", response_model=list[RegistrationRead], tags=["participant"])
def my_registrations(user: CurrentUser, db: DbSession) -> list[RegistrationRead]:
    return [services.to_registration(r) for r in services.list_my_registrations(db, user)]


@app.post(
    "/registrations/{registration_id}/cancel",
    response_model=RegistrationRead,
    tags=["participant"],
)
def cancel_registration(registration_id: int, user: CurrentUser, db: DbSession) -> RegistrationRead:
    return services.to_registration(services.cancel_registration(db, user, registration_id))
