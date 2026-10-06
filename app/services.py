from typing import TypeVar

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent,
    CancelReason,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    Role,
    User,
    utc_now,
)
from app.schemas import (
    EventCreate,
    EventOrganizerRead,
    EventPublic,
    EventUpdate,
    ParticipantRead,
    RegistrationRead,
)

Entity = TypeVar("Entity")


def _commit_and_refresh(db: Session, entity: Entity) -> Entity:
    db.commit()
    db.refresh(entity)
    return entity


def _not_found(what: str = "Event") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def require_role(user: User, role: Role) -> None:
    if user.role != role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")


def get_visible_event(db: Session, event_id: int, user: User | None) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise _not_found()
    if event.status == EventStatus.DRAFT and (user is None or user.id != event.owner_id):
        raise _not_found()
    return event


def get_owned_event(db: Session, event_id: int, user: User) -> Event:
    require_role(user, Role.ORGANIZER)
    event = db.get(Event, event_id)
    if event is None or event.owner_id != user.id:
        raise _not_found()
    return event


def get_own_registration(db: Session, registration_id: int, user: User) -> Registration:
    require_role(user, Role.PARTICIPANT)
    registration = db.get(Registration, registration_id)
    if registration is None or registration.participant_id != user.id:
        raise _not_found("Registration")
    return registration


def to_public(event: Event) -> EventPublic:
    return EventPublic(
        id=event.id,
        title=event.title,
        description=event.description,
        location=event.location,
        starts_at=event.starts_at,
        status=event.status,
        capacity=event.capacity,
        free_places=event.capacity - event.reserved,
    )


def to_organizer(event: Event) -> EventOrganizerRead:
    return EventOrganizerRead(
        **to_public(event).model_dump(),
        owner_id=event.owner_id,
        reserved=event.reserved,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def to_registration(registration: Registration) -> RegistrationRead:
    event = registration.event
    return RegistrationRead(
        id=registration.id,
        event_id=event.id,
        event_title=event.title,
        event_starts_at=event.starts_at,
        event_status=event.status,
        status=registration.status,
        created_at=registration.created_at,
        cancelled_at=registration.cancelled_at,
        cancel_reason=registration.cancel_reason,
    )


def audit(
    db: Session, event_id: int, actor_id: int, action: str, registration_id: int | None = None
) -> None:
    db.add(
        AuditEvent(
            event_id=event_id, actor_id=actor_id, action=action, registration_id=registration_id
        )
    )


def list_audit(db: Session, user: User, event_id: int) -> list[AuditEvent]:
    event = get_owned_event(db, event_id, user)
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_id == event.id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        ).all()
    )


def list_public_events(db: Session, limit: int, offset: int) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .where(Event.status == EventStatus.PUBLISHED, Event.starts_at > utc_now())
            .order_by(Event.starts_at, Event.id)
            .limit(limit)
            .offset(offset)
        ).all()
    )


def list_my_events(db: Session, user: User) -> list[Event]:
    require_role(user, Role.ORGANIZER)
    return list(
        db.scalars(
            select(Event).where(Event.owner_id == user.id).order_by(Event.starts_at, Event.id)
        ).all()
    )


def list_my_registrations(db: Session, user: User) -> list[Registration]:
    require_role(user, Role.PARTICIPANT)
    return list(
        db.scalars(
            select(Registration)
            .where(Registration.participant_id == user.id)
            .order_by(Registration.created_at, Registration.id)
        ).all()
    )


def list_participants(db: Session, user: User, event_id: int) -> list[ParticipantRead]:
    event = get_owned_event(db, event_id, user)
    rows = db.execute(
        select(Registration, User)
        .join(User, Registration.participant_id == User.id)
        .where(
            Registration.event_id == event.id,
            Registration.status == RegistrationStatus.ACTIVE,
        )
        .order_by(Registration.created_at, Registration.id)
    ).all()
    return [
        ParticipantRead(
            registration_id=registration.id,
            participant_id=participant.id,
            email=participant.email,
            display_name=participant.display_name,
            registered_at=registration.created_at,
        )
        for registration, participant in rows
    ]


def count_active_registrations(db: Session, event_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Registration)
            .where(
                Registration.event_id == event_id,
                Registration.status == RegistrationStatus.ACTIVE,
            )
        )
        or 0
    )


def create_event(db: Session, user: User, payload: EventCreate) -> Event:
    require_role(user, Role.ORGANIZER)
    event = Event(
        owner_id=user.id,
        title=payload.title,
        description=payload.description,
        location=payload.location,
        starts_at=payload.starts_at,
        capacity=payload.capacity,
        reserved=0,
        status=EventStatus.DRAFT,
    )
    db.add(event)
    db.flush()
    audit(db, event.id, user.id, "event_created")
    return _commit_and_refresh(db, event)


def update_event(db: Session, user: User, event_id: int, payload: EventUpdate) -> Event:
    event = get_owned_event(db, event_id, user)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return event

    statement = update(Event).where(
        Event.id == event.id,
        Event.owner_id == user.id,
        Event.status.in_([EventStatus.DRAFT, EventStatus.PUBLISHED]),
    )
    if "capacity" in changes:
        statement = statement.where(Event.reserved <= changes["capacity"])

    result = db.execute(statement.values(**changes).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        db.refresh(event)
        if event.status == EventStatus.CANCELLED:
            raise _conflict("A cancelled event cannot be changed")
        raise _conflict("Capacity cannot be lower than the number of active registrations")

    audit(db, event.id, user.id, "capacity_changed" if "capacity" in changes else "event_updated")
    return _commit_and_refresh(db, event)


def publish_event(db: Session, user: User, event_id: int) -> Event:
    event = get_owned_event(db, event_id, user)
    result = db.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.owner_id == user.id,
            Event.status == EventStatus.DRAFT,
            Event.starts_at > utc_now(),
        )
        .values(status=EventStatus.PUBLISHED)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        db.refresh(event)
        if event.status != EventStatus.DRAFT:
            raise _conflict("Only a draft event can be published")
        raise _conflict("An event that has already started cannot be published")

    audit(db, event.id, user.id, "event_published")
    return _commit_and_refresh(db, event)


def cancel_event(db: Session, user: User, event_id: int) -> Event:
    event = get_owned_event(db, event_id, user)
    now = utc_now()
    result = db.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.owner_id == user.id,
            Event.status == EventStatus.PUBLISHED,
        )
        .values(status=EventStatus.CANCELLED, reserved=0)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise _conflict("Only a published event can be cancelled")

    registration_ids = list(
        db.scalars(
            select(Registration.id).where(
                Registration.event_id == event.id,
                Registration.status == RegistrationStatus.ACTIVE,
            )
        ).all()
    )
    db.execute(
        update(Registration)
        .where(
            Registration.event_id == event.id,
            Registration.status == RegistrationStatus.ACTIVE,
        )
        .values(
            status=RegistrationStatus.CANCELLED,
            cancelled_at=now,
            cancelled_by_id=user.id,
            cancel_reason=CancelReason.EVENT_CANCELLED,
        )
        .execution_options(synchronize_session=False)
    )
    audit(db, event.id, user.id, "event_cancelled")
    for registration_id in registration_ids:
        audit(db, event.id, user.id, "registration_cancelled_with_event", registration_id)
    return _commit_and_refresh(db, event)


def _registration_refusal(db: Session, event_id: int) -> HTTPException:
    event = db.get(Event, event_id)
    if event is None or event.status == EventStatus.DRAFT:
        return _not_found()
    if event.status == EventStatus.CANCELLED:
        return _conflict("Event is cancelled")
    if event.starts_at <= utc_now():
        return _conflict("Registration is closed: the event has already started")
    return _conflict("No free places")


def register(db: Session, user: User, event_id: int) -> Registration:
    require_role(user, Role.PARTICIPANT)
    result = db.execute(
        update(Event)
        .where(
            Event.id == event_id,
            Event.status == EventStatus.PUBLISHED,
            Event.starts_at > utc_now(),
            Event.reserved < Event.capacity,
        )
        .values(reserved=Event.reserved + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise _registration_refusal(db, event_id)

    registration = Registration(
        event_id=event_id,
        participant_id=user.id,
        status=RegistrationStatus.ACTIVE,
    )
    db.add(registration)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _conflict("You already have an active registration for this event") from None

    audit(db, event_id, user.id, "registration_created", registration.id)
    return _commit_and_refresh(db, registration)


def cancel_registration(db: Session, user: User, registration_id: int) -> Registration:
    registration = get_own_registration(db, registration_id, user)
    if registration.status != RegistrationStatus.ACTIVE:
        raise _conflict("Registration is not active")
    if registration.event.starts_at <= utc_now():
        raise _conflict("The event has already started")

    now = utc_now()
    result = db.execute(
        update(Registration)
        .where(
            Registration.id == registration.id,
            Registration.participant_id == user.id,
            Registration.status == RegistrationStatus.ACTIVE,
        )
        .values(
            status=RegistrationStatus.CANCELLED,
            cancelled_at=now,
            cancelled_by_id=user.id,
            cancel_reason=CancelReason.PARTICIPANT,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise _conflict("Registration is not active")

    db.execute(
        update(Event)
        .where(Event.id == registration.event_id, Event.reserved > 0)
        .values(reserved=Event.reserved - 1)
        .execution_options(synchronize_session=False)
    )
    audit(db, registration.event_id, user.id, "registration_cancelled", registration.id)
    db.commit()
    db.refresh(registration)
    db.refresh(registration.event)
    return registration
