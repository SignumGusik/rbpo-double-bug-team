from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app import services
from app.config import get_settings
from app.database import SessionLocal
from app.main import seed_demo_users
from app.models import Event, Role, UTCDateTime, User
from app.security import create_access_token
from conftest import create_event, future, login

ORG1 = "organizer1@campus.local"
P1 = "participant1@campus.local"


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("JWT_SECRET", None, "JWT_SECRET must be set"),
        ("JWT_SECRET", "short", "JWT_SECRET must contain at least 32 characters"),
        ("SEED_PASSWORD", None, "SEED_PASSWORD must be set"),
        ("SEED_PASSWORD", "short", "SEED_PASSWORD must contain at least 10 characters"),
    ],
)
def test_settings_reject_missing_or_weak_secrets(monkeypatch, variable, value, message):
    monkeypatch.setenv("JWT_SECRET", "j" * 32)
    monkeypatch.setenv("SEED_PASSWORD", "p" * 10)
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)

    with pytest.raises(RuntimeError, match=message):
        get_settings()


def test_settings_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "j" * 32)
    monkeypatch.setenv("SEED_PASSWORD", "p" * 10)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ACCESS_TOKEN_MINUTES", raising=False)

    defaults = get_settings()
    assert defaults.database_url == "sqlite:///./campus_events.db"
    assert defaults.access_token_minutes == 30

    monkeypatch.setenv("DATABASE_URL", "sqlite:///custom.db")
    monkeypatch.setenv("ACCESS_TOKEN_MINUTES", "15")
    overridden = get_settings()
    assert overridden.database_url == "sqlite:///custom.db"
    assert overridden.access_token_minutes == 15


def test_utc_datetime_conversion_and_validation():
    column_type = UTCDateTime()
    source = datetime(2030, 1, 2, 12, tzinfo=timezone(timedelta(hours=3)))

    assert column_type.process_bind_param(None, None) is None
    assert column_type.process_bind_param(source, None) == datetime(2030, 1, 2, 9)
    assert column_type.process_result_value(None, None) is None
    assert column_type.process_result_value(datetime(2030, 1, 2, 9), None) == datetime(
        2030, 1, 2, 9, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="Naive datetime values are not allowed"):
        column_type.process_bind_param(datetime(2030, 1, 2, 9), None)


def test_seed_is_idempotent_and_login_failures_are_indistinguishable(client):
    with SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(User))
        seed_demo_users(db)
        after = db.scalar(select(func.count()).select_from(User))
    assert before == after == 5

    unknown = client.post(
        "/auth/login", json={"email": "missing@campus.local", "password": "test-password-123"}
    )
    wrong_password = client.post(
        "/auth/login", json={"email": P1, "password": "incorrect-password"}
    )
    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json() == wrong_password.json()


def test_changed_authentication_context_is_rejected(client):
    headers = login(client, P1)
    participant_id = client.get("/users/me", headers=headers).json()["id"]

    with SessionLocal() as db:
        user = db.get(User, participant_id)
        user.is_active = False
        db.commit()

    assert client.get("/users/me", headers=headers).status_code == 401

    with SessionLocal() as db:
        user = db.get(User, participant_id)
        user.is_active = True
        db.commit()

    mismatched = create_access_token(participant_id, Role.ORGANIZER)
    response = client.get("/users/me", headers={"Authorization": f"Bearer {mismatched}"})
    assert response.status_code == 401

    missing = create_access_token(999_999, Role.PARTICIPANT)
    response = client.get("/users/me", headers={"Authorization": f"Bearer {missing}"})
    assert response.status_code == 401


def test_public_event_listing_filters_and_paginates(client):
    organizer = login(client, ORG1)
    create_event(client, organizer, publish=False)
    published_id = create_event(client, organizer)

    response = client.get("/events?limit=1&offset=0")
    assert response.status_code == 200
    assert [event["id"] for event in response.json()] == [published_id]
    assert client.get("/events?limit=1&offset=1").json() == []


def test_event_update_and_invalid_state_paths(client):
    organizer = login(client, ORG1)
    event_id = create_event(client, organizer, publish=False)

    unchanged = client.patch(f"/events/{event_id}", headers=organizer, json={})
    assert unchanged.status_code == 200
    assert unchanged.json()["title"] == "Security meetup"

    renamed = client.patch(
        f"/events/{event_id}", headers=organizer, json={"title": "Updated meetup"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Updated meetup"

    rescheduled = client.patch(
        f"/events/{event_id}", headers=organizer, json={"starts_at": future(14)}
    )
    assert rescheduled.status_code == 200
    assert client.patch(
        f"/events/{event_id}", headers=organizer, json={"description": None}
    ).status_code == 422

    assert client.post(f"/events/{event_id}/publish", headers=organizer).status_code == 200
    assert client.post(f"/events/{event_id}/cancel", headers=organizer).status_code == 200
    cancelled_update = client.patch(
        f"/events/{event_id}", headers=organizer, json={"title": "Too late"}
    )
    assert cancelled_update.status_code == 409

    started_draft_id = create_event(client, organizer, publish=False)
    with SessionLocal() as db:
        db.execute(
            update(Event)
            .where(Event.id == started_draft_id)
            .values(starts_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        db.commit()
    assert client.post(
        f"/events/{started_draft_id}/publish", headers=organizer
    ).status_code == 409

    draft_id = create_event(client, organizer, publish=False)
    assert client.post(f"/events/{draft_id}/cancel", headers=organizer).status_code == 409


def test_cancelled_registration_cannot_be_cancelled_twice(client):
    organizer = login(client, ORG1)
    participant = login(client, P1)
    event_id = create_event(client, organizer)
    registration_id = client.post(
        f"/events/{event_id}/registrations", headers=participant
    ).json()["id"]

    assert client.post(
        f"/registrations/{registration_id}/cancel", headers=participant
    ).status_code == 200
    second = client.post(f"/registrations/{registration_id}/cancel", headers=participant)
    assert second.status_code == 409
    assert second.json()["detail"] == "Registration is not active"


def test_concurrent_cancellation_conflict_is_reported(client, monkeypatch):
    organizer = login(client, ORG1)
    participant = login(client, P1)
    participant_id = client.get("/users/me", headers=participant).json()["id"]
    event_id = create_event(client, organizer)
    registration_id = client.post(
        f"/events/{event_id}/registrations", headers=participant
    ).json()["id"]

    with SessionLocal() as db:
        user = db.get(User, participant_id)
        registration = services.get_own_registration(db, registration_id, user)
        assert registration.event.starts_at > datetime.now(UTC)
        monkeypatch.setattr(db, "execute", lambda statement: SimpleNamespace(rowcount=0))
        with pytest.raises(HTTPException) as error:
            services.cancel_registration(db, user, registration_id)

    assert error.value.status_code == 409
    assert error.value.detail == "Registration is not active"
