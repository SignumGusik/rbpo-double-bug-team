import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TEST_DB = Path(tempfile.mkdtemp()) / "test_campus_events.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["JWT_SECRET"] = "test-only-secret-0123456789-abcdefghijklmnop"
os.environ["SEED_PASSWORD"] = "test-password-123"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

PASSWORD = os.environ["SEED_PASSWORD"]


@pytest.fixture
def client():
    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def future(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def create_event(
    client: TestClient, headers: dict[str, str], capacity: int = 2, publish: bool = True
) -> int:
    response = client.post(
        "/events",
        json={
            "title": "Security meetup",
            "description": "Demo event",
            "location": "Room 101",
            "starts_at": future(),
            "capacity": capacity,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    event_id = response.json()["id"]
    if publish:
        published = client.post(f"/events/{event_id}/publish", headers=headers)
        assert published.status_code == 200, published.text
    return event_id
