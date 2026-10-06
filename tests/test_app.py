import os
import threading
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException
from sqlalchemy import select, update

from app import services
from app.database import SessionLocal
from app.models import AuditEvent, Event, Registration, RegistrationStatus, Role, User
from conftest import create_event, future, login

ORG1 = "organizer1@campus.local"
ORG2 = "organizer2@campus.local"
P1 = "participant1@campus.local"
P2 = "participant2@campus.local"
P3 = "participant3@campus.local"


def user_id(client, headers) -> int:
    return client.get("/users/me", headers=headers).json()["id"]


def test_health_and_authentication_required(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/events", json={}).status_code == 401
    assert client.get("/my/registrations").status_code == 401
    assert client.get("/users/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_forged_and_expired_tokens_are_rejected(client):
    p1 = login(client, P1)
    uid = user_id(client, p1)
    now = datetime.now(UTC)
    claims = {"sub": str(uid), "role": "organizer", "iat": now, "iss": "campus-events"}

    forged = jwt.encode({**claims, "exp": now + timedelta(minutes=30)},
                        "attacker-secret-attacker-secret-attacker", algorithm="HS256")
    assert client.get("/users/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401

    expired = jwt.encode({**claims, "role": "participant", "exp": now - timedelta(minutes=1)},
                         os.environ["JWT_SECRET"], algorithm="HS256")
    assert client.get("/users/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_identity_comes_from_token_not_from_body(client):
    org1, p1, p2 = login(client, ORG1), login(client, P1), login(client, P2)
    event_id = create_event(client, org1)
    p2_id = user_id(client, p2)

    response = client.post(f"/events/{event_id}/registrations",
                           json={"participant_id": p2_id}, headers=p1)
    assert response.status_code == 201
    assert len(client.get("/my/registrations", headers=p1).json()) == 1
    assert client.get("/my/registrations", headers=p2).json() == []

    bad = client.post("/events", headers=org1, json={
        "title": "x" * 5, "location": "Hall", "starts_at": future(), "capacity": 5,
        "owner_id": 999,
    })
    assert bad.status_code == 422

    denied = client.post("/events", headers=p1, json={
        "title": "Party", "location": "Hall", "starts_at": future(), "capacity": 5,
    })
    assert denied.status_code == 403


def test_passwords_are_hashed_and_token_has_no_secrets(client):
    with SessionLocal() as db:
        hashes = db.scalars(select(User.password_hash)).all()
    assert hashes and all(h.startswith("$argon2") for h in hashes)
    assert all("test-password-123" not in h for h in hashes)

    token = login(client, P1)["Authorization"].split()[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) == {"sub", "role", "iat", "exp", "iss"}


def test_organizer_cannot_manage_foreign_event(client):
    org1, org2 = login(client, ORG1), login(client, ORG2)
    event_id = create_event(client, org1)

    assert client.patch(f"/events/{event_id}", json={"title": "Hacked"}, headers=org2).status_code == 404
    assert client.post(f"/events/{event_id}/cancel", headers=org2).status_code == 404
    assert client.get(f"/events/{event_id}").json()["title"] == "Security meetup"
    assert client.get(f"/events/{event_id}").json()["status"] == "published"


def test_participant_cannot_cancel_foreign_registration(client):
    org1, p1, p2 = login(client, ORG1), login(client, P1), login(client, P2)
    event_id = create_event(client, org1)
    registration_id = client.post(f"/events/{event_id}/registrations", headers=p1).json()["id"]

    assert client.post(f"/registrations/{registration_id}/cancel", headers=p2).status_code == 404
    assert client.get("/my/registrations", headers=p1).json()[0]["status"] == "active"


def test_draft_is_visible_only_to_owner(client):
    org1, org2, p1 = login(client, ORG1), login(client, ORG2), login(client, P1)
    draft_id = create_event(client, org1, publish=False)

    assert client.get(f"/events/{draft_id}", headers=org1).status_code == 200
    for headers in ({}, org2, p1):
        response = client.get(f"/events/{draft_id}", headers=headers)
        assert response.status_code == 404
        assert response.json() == client.get("/events/999999", headers=headers).json()
    assert client.post(f"/events/{draft_id}/registrations", headers=p1).status_code == 404


def test_participant_list_is_owner_only(client):
    org1, org2, p1, p2 = (login(client, e) for e in (ORG1, ORG2, P1, P2))
    event_id = create_event(client, org1)
    client.post(f"/events/{event_id}/registrations", headers=p1)
    client.post(f"/events/{event_id}/registrations", headers=p2)

    owner_view = client.get(f"/events/{event_id}/participants", headers=org1)
    assert owner_view.status_code == 200
    assert {p["email"] for p in owner_view.json()} == {P1, P2}

    assert client.get(f"/events/{event_id}/participants", headers=org2).status_code == 404
    assert client.get(f"/events/{event_id}/participants", headers=p1).status_code == 403
    assert client.get(f"/events/{event_id}/participants").status_code == 401

    for headers in ({}, p2):
        text = client.get(f"/events/{event_id}", headers=headers).text
        assert P1 not in text and "participant" not in text.lower()
    assert P1 not in client.get("/my/registrations", headers=p2).text


def test_capacity_is_enforced_and_seat_is_released(client):
    org1, p1, p2, p3 = (login(client, e) for e in (ORG1, P1, P2, P3))
    event_id = create_event(client, org1, capacity=2)

    r1 = client.post(f"/events/{event_id}/registrations", headers=p1)
    assert r1.status_code == 201
    assert client.post(f"/events/{event_id}/registrations", headers=p2).status_code == 201
    full = client.post(f"/events/{event_id}/registrations", headers=p3)
    assert full.status_code == 409 and full.json()["detail"] == "No free places"
    assert client.get(f"/events/{event_id}").json()["free_places"] == 0

    assert client.post(f"/registrations/{r1.json()['id']}/cancel", headers=p1).status_code == 200
    assert client.post(f"/events/{event_id}/registrations", headers=p3).status_code == 201


def test_concurrent_registrations_do_not_overbook(client):
    org1 = login(client, ORG1)
    capacity, contenders = 3, 12
    event_id = create_event(client, org1, capacity=capacity)

    with SessionLocal() as db:
        users = [User(email=f"load{i}@campus.local", display_name=f"Load {i}",
                      password_hash="not-used", role=Role.PARTICIPANT) for i in range(contenders)]
        db.add_all(users)
        db.commit()
        ids = [u.id for u in users]

    barrier = threading.Barrier(contenders)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker(uid: int) -> None:
        with SessionLocal() as db:
            user = db.get(User, uid)
            barrier.wait()
            try:
                services.register(db, user, event_id)
                outcome: object = "ok"
            except HTTPException as exc:
                outcome = exc.status_code
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == capacity
    assert outcomes.count(409) == contenders - capacity
    with SessionLocal() as db:
        assert db.get(Event, event_id).reserved == capacity
        assert services.count_active_registrations(db, event_id) == capacity


def test_concurrent_duplicate_registration_rolls_back_reserved_seat(client):
    organizer, participant = login(client, ORG1), login(client, P1)
    event_id = create_event(client, organizer, capacity=2)
    participant_id = user_id(client, participant)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        with SessionLocal() as db:
            user = db.get(User, participant_id)
            barrier.wait()
            try:
                services.register(db, user, event_id)
                outcome: object = "ok"
            except HTTPException as exc:
                outcome = exc.status_code
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count(409) == 1
    with SessionLocal() as db:
        assert db.get(Event, event_id).reserved == 1
        assert services.count_active_registrations(db, event_id) == 1


def test_capacity_cannot_drop_below_reserved(client):
    org1, p1, p2 = login(client, ORG1), login(client, P1), login(client, P2)
    event_id = create_event(client, org1, capacity=3)
    client.post(f"/events/{event_id}/registrations", headers=p1)
    client.post(f"/events/{event_id}/registrations", headers=p2)

    assert client.patch(f"/events/{event_id}", json={"capacity": 1}, headers=org1).status_code == 409
    assert client.patch(f"/events/{event_id}", json={"capacity": 2}, headers=org1).status_code == 200
    assert client.patch(f"/events/{event_id}", json={"reserved": 0}, headers=org1).status_code == 422


def test_state_rules(client):
    org1, p1, p2 = login(client, ORG1), login(client, P1), login(client, P2)
    event_id = create_event(client, org1, capacity=5)

    assert client.post(f"/events/{event_id}/publish", headers=org1).status_code == 409
    assert client.patch(f"/events/{event_id}", json={"status": "draft"}, headers=org1).status_code == 422

    assert client.post(f"/events/{event_id}/registrations", headers=p1).status_code == 201
    dup = client.post(f"/events/{event_id}/registrations", headers=p1)
    assert dup.status_code == 409
    assert client.get(f"/events/{event_id}").json()["free_places"] == 4
    client.post(f"/events/{event_id}/registrations", headers=p2)

    assert client.post(f"/events/{event_id}/cancel", headers=org1).status_code == 200
    for headers in (p1, p2):
        reg = client.get("/my/registrations", headers=headers).json()[0]
        assert reg["status"] == "cancelled" and reg["cancel_reason"] == "event_cancelled"
    assert client.post(f"/events/{event_id}/registrations", headers=login(client, P3)).status_code == 409
    assert client.post(f"/events/{event_id}/publish", headers=org1).status_code == 409


def test_started_event_closes_registration_and_cancellation(client):
    org1, p1, p2 = login(client, ORG1), login(client, P1), login(client, P2)
    event_id = create_event(client, org1, capacity=5)
    registration_id = client.post(f"/events/{event_id}/registrations", headers=p1).json()["id"]

    with SessionLocal() as db:
        db.execute(update(Event).where(Event.id == event_id)
                   .values(starts_at=datetime.now(UTC) - timedelta(minutes=1)))
        db.commit()

    assert client.post(f"/events/{event_id}/registrations", headers=p2).status_code == 409
    assert client.post(f"/registrations/{registration_id}/cancel", headers=p1).status_code == 409


def test_input_validation(client):
    org1 = login(client, ORG1)
    base = {"title": "Valid title", "location": "Hall", "starts_at": future(), "capacity": 10}

    assert client.post("/events", headers=org1, json={**base, "title": "x" * 120}).status_code == 201
    for bad in (
        {"title": "x" * 121},
        {"title": ""},
        {"capacity": 0},
        {"capacity": 10001},
        {"capacity": "many"},
        {"starts_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        {"starts_at": "2030-01-01T10:00:00"},
    ):
        assert client.post("/events", headers=org1, json={**base, **bad}).status_code == 422, bad
    assert client.get("/events?limit=1000").status_code == 422

    injection = "Talk'); DROP TABLE events; --"
    created = client.post("/events", headers=org1, json={**base, "title": injection})
    assert created.status_code == 201 and created.json()["title"] == injection
    assert len(client.get("/my/events", headers=org1).json()) == 2


def test_audit_records_actor_and_is_read_only(client):
    org1, p1 = login(client, ORG1), login(client, P1)
    event_id = create_event(client, org1)
    registration_id = client.post(f"/events/{event_id}/registrations", headers=p1).json()["id"]
    client.post(f"/registrations/{registration_id}/cancel", headers=p1)
    client.post(f"/events/{event_id}/cancel", headers=org1)

    journal = client.get(f"/events/{event_id}/audit", headers=org1).json()
    actions = [(e["action"], e["actor_id"]) for e in journal]
    org_id, p1_id = user_id(client, org1), user_id(client, p1)
    assert actions == [
        ("event_created", org_id),
        ("event_published", org_id),
        ("registration_created", p1_id),
        ("registration_cancelled", p1_id),
        ("event_cancelled", org_id),
    ]
    assert client.delete(f"/events/{event_id}/audit", headers=org1).status_code == 405
    assert client.get(f"/events/{event_id}/audit", headers=p1).status_code == 403

    with SessionLocal() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "registration_created")).registration_id == registration_id
        assert db.get(Registration, registration_id).status == RegistrationStatus.CANCELLED


def test_event_cancellation_audit_identifies_affected_registration(client):
    organizer, participant = login(client, ORG1), login(client, P1)
    event_id = create_event(client, organizer)
    registration_id = client.post(
        f"/events/{event_id}/registrations", headers=participant
    ).json()["id"]

    assert client.post(f"/events/{event_id}/cancel", headers=organizer).status_code == 200
    journal = client.get(f"/events/{event_id}/audit", headers=organizer).json()
    cancellation = next(
        entry for entry in journal if entry["action"] == "registration_cancelled_with_event"
    )
    assert cancellation["registration_id"] == registration_id

    with SessionLocal() as db:
        registration = db.get(Registration, registration_id)
        assert registration.cancelled_by_id == user_id(client, organizer)
        assert registration.cancel_reason.value == "event_cancelled"
