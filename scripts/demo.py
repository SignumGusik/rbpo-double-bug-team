import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
PASSWORD = os.environ["SEED_PASSWORD"]
results: list[bool] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def check(title: str, ok: bool, details: object = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {title}" + (f"  ->  {details}" if details != "" else ""))


def login(email: str) -> str:
    status, body = call("POST", "/auth/login", body={"email": email, "password": PASSWORD})
    assert status == 200, (email, status, body)
    return body["access_token"]


def new_event(token: str, capacity: int, title: str) -> int:
    starts_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    status, body = call("POST", "/events", token, {
        "title": title, "description": "Demo", "location": "Room 101",
        "starts_at": starts_at, "capacity": capacity,
    })
    assert status == 201, body
    return body["id"]


def main() -> None:
    print(f"== Campus Events demo against {BASE_URL}\n")
    check("Health-check", call("GET", "/health") == (200, {"status": "ok"}))

    org1, org2 = login("organizer1@campus.local"), login("organizer2@campus.local")
    p1, p2, p3 = (login(f"participant{i}@campus.local") for i in (1, 2, 3))

    print("\n-- Scenario 1: organizer creates and publishes an event")
    event_id = new_event(org1, 2, "Security meetup")
    check("Draft is hidden from the public (SR-02)", call("GET", f"/events/{event_id}")[0] == 404)
    check("Draft is hidden from another organizer (SR-02)",
          call("GET", f"/events/{event_id}", org2)[0] == 404)
    status, body = call("POST", f"/events/{event_id}/publish", org1)
    check("Owner publishes the event (SR-05)", status == 200 and body["status"] == "published")
    check("Second publish is refused (SR-05)", call("POST", f"/events/{event_id}/publish", org1)[0] == 409)
    check("Foreign organizer cannot cancel it (SR-02)",
          call("POST", f"/events/{event_id}/cancel", org2)[0] == 404)

    print("\n-- Scenario 2: registration with limited capacity (2 places)")
    s1, r1 = call("POST", f"/events/{event_id}/registrations", p1)
    s2, _ = call("POST", f"/events/{event_id}/registrations", p2)
    check("Participant 1 and 2 register", s1 == 201 and s2 == 201)
    s3, b3 = call("POST", f"/events/{event_id}/registrations", p3)
    check("Participant 3 gets 'No free places' (SR-04)", s3 == 409, b3)
    check("Duplicate registration is refused (SR-05)",
          call("POST", f"/events/{event_id}/registrations", p1)[0] == 409)

    status, public = call("GET", f"/events/{event_id}")
    check("Public card shows only aggregates, no e-mails (SR-03)",
          status == 200 and "participant1@campus.local" not in json.dumps(public), public)
    status, plist = call("GET", f"/events/{event_id}/participants", org1)
    check("Owner sees the participant list (SR-03)", status == 200 and len(plist) == 2,
          [p["email"] for p in plist])
    check("Another organizer cannot see it (SR-03)",
          call("GET", f"/events/{event_id}/participants", org2)[0] == 404)
    check("Participant cannot see it (SR-03)",
          call("GET", f"/events/{event_id}/participants", p2)[0] == 403)

    print("\n-- Attacks on identity and objects")
    check("Participant 2 cannot cancel participant 1's registration (SR-02)",
          call("POST", f"/registrations/{r1['id']}/cancel", p2)[0] == 404)
    check("Participant cannot create an event (SR-01)", call("POST", "/events", p1, {
        "title": "Fake", "location": "X", "capacity": 5,
        "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()})[0] == 403)
    check("Injected owner_id is rejected (SR-01)", call("POST", "/events", org1, {
        "title": "Fake", "location": "X", "capacity": 5, "owner_id": 1,
        "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()})[0] == 422)
    check("Client cannot set the seat counter (SR-04)",
          call("PATCH", f"/events/{event_id}", org1, {"reserved": 0})[0] == 422)
    check("Capacity cannot drop below taken seats (SR-04)",
          call("PATCH", f"/events/{event_id}", org1, {"capacity": 1})[0] == 409)
    check("Tampered token is rejected (SR-08)", call("GET", "/users/me", p1[:-4] + "AAAA")[0] == 401)

    print("\n-- Scenario 3: cancellation of participation and of the event")
    check("Participant 1 cancels own registration",
          call("POST", f"/registrations/{r1['id']}/cancel", p1)[0] == 200)
    check("The released place is taken by participant 3",
          call("POST", f"/events/{event_id}/registrations", p3)[0] == 201)
    check("Organizer cancels the event", call("POST", f"/events/{event_id}/cancel", org1)[0] == 200)
    regs = call("GET", "/my/registrations", p2)[1]
    check("Participant 2 sees registration cancelled with the event",
          regs[-1]["status"] == "cancelled" and regs[-1]["cancel_reason"] == "event_cancelled")
    check("No registration on a cancelled event",
          call("POST", f"/events/{event_id}/registrations", p1)[0] == 409)

    status, journal = call("GET", f"/events/{event_id}/audit", org1)
    check("Audit journal is available to the owner (SR-07)", status == 200)
    for entry in journal:
        print(f"      {entry['created_at']}  actor={entry['actor_id']}  {entry['action']}")

    print("\n-- Race: 3 participants press 'register' at once, only 2 places (T-01)")
    race_id = new_event(org1, 2, "Race test")
    call("POST", f"/events/{race_id}/publish", org1)
    barrier, codes = threading.Barrier(3), []

    def attempt(token: str) -> None:
        barrier.wait()
        codes.append(call("POST", f"/events/{race_id}/registrations", token)[0])

    threads = [threading.Thread(target=attempt, args=(t,)) for t in (p1, p2, p3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    status, plist = call("GET", f"/events/{race_id}/participants", org1)
    check("Exactly 2 succeeded, 1 got 409, list has 2 people (SR-04)",
          sorted(codes) == [201, 201, 409] and len(plist) == 2, sorted(codes))

    print(f"\n== {sum(results)}/{len(results)} checks passed")


if __name__ == "__main__":
    main()
