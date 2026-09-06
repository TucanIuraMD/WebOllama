"""Agents admin UI — backend behind "+ Add Agent" / "+ Add Capability".

Covers the API surface the new UI relies on:
- admin creates an agent / capability at runtime (no restart) and it shows up
  in GET /api/agents and in the matrix payload;
- non-admin is rejected with 403;
- duplicate slug (and duplicate name, the UNIQUE-constraint path) -> 400;
- assessments can still be created/updated/reset against freshly created
  config rows, and a reloaded matrix returns the saved state (note included).

Notes on isolation: same approach as test_agents.py — unique names per test,
delta assertions on counts, and process-wide rate limiters swapped for
high-capacity instances so this module neither hits 429 nor drains budget.
"""
import uuid

import pytest


@pytest.fixture(autouse=True)
def _isolate_rate_limiters(monkeypatch):
    from webui import deps, security
    from webui.security import RateLimiter

    monkeypatch.setattr(deps, "_dangerous_limiter", RateLimiter(10_000, 1))
    monkeypatch.setattr(deps, "_general_limiter", RateLimiter(10_000, 1))
    security._rate.clear()
    yield
    security._rate.clear()


def _uname(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _login(client, username="admin", password="changeme"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client.cookies.get("session")


def _make_non_admin(client):
    """Admin creates a plain user; returns (session token, username)."""
    username = _uname("viewer")
    r = client.post(
        "/api/auth/users",
        json={"username": username, "password": "testpass123", "role": "user"},
    )
    assert r.status_code == 200, r.text
    return _login(client, username, "testpass123"), username


def test_admin_creates_agent_and_capability(client):
    admin = _login(client)
    auth = {"cookies": {"session": admin}}

    agents_before = len(client.get("/api/agents", **auth).json()["agents"])

    name = _uname("Automation")
    r = client.post(
        "/api/agents/agents",
        json={"name": name, "description": "runtime-created env", "enabled": True},
        **auth,
    )
    assert r.status_code == 200, r.text
    agent = r.json()["agent"]
    assert agent["name"] == name
    assert agent["slug"].startswith("automation-")  # auto-derived from name
    assert agent["enabled"] == 1
    assert agent["id"] > 0

    # config list and matrix both see it immediately — no restart
    listing = client.get("/api/agents", **auth).json()
    assert len(listing["agents"]) == agents_before + 1
    assert any(a["slug"] == agent["slug"] for a in listing["agents"])

    matrix = client.get("/api/agents/matrix", **auth).json()
    assert any(a["slug"] == agent["slug"] for a in matrix["agents"])
    assert matrix["counts"]["agents"] == agents_before + 1

    cap_name = _uname("OCR")
    r = client.post("/api/agents/capabilities", json={"name": cap_name}, **auth)
    assert r.status_code == 200, r.text
    cap = r.json()["capability"]
    assert cap["slug"].startswith("ocr-")

    caps = client.get("/api/agents/capabilities", **auth).json()["capabilities"]
    assert any(c["slug"] == cap["slug"] for c in caps)
    matrix = client.get("/api/agents/matrix", **auth).json()
    assert any(c["slug"] == cap["slug"] for c in matrix["capabilities"])


def test_non_admin_cannot_create_agent_or_capability(client):
    admin = _login(client)
    viewer, _ = _make_non_admin(client)
    vauth = {"cookies": {"session": viewer}}
    aauth = {"cookies": {"session": admin}}

    assert client.get("/api/agents", **vauth).status_code == 200  # read is fine
    r = client.post("/api/agents/agents", json={"name": _uname("Nope")}, **vauth)
    assert r.status_code == 403
    r = client.post("/api/agents/capabilities", json={"name": _uname("Nope")}, **vauth)
    assert r.status_code == 403

    # sanity: admin is allowed on the very same endpoints
    assert client.post("/api/agents/agents", json={"name": _uname("Yes")}, **aauth).status_code == 200


def test_duplicate_slug_and_name_rejected(client):
    admin = _login(client)
    auth = {"cookies": {"session": admin}}
    slug = _uname("dup-slug")

    r = client.post("/api/agents/agents", json={"name": _uname("First"), "slug": slug}, **auth)
    assert r.status_code == 200
    # same slug again -> 400, not 500
    r = client.post("/api/agents/agents", json={"name": _uname("Second"), "slug": slug}, **auth)
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"].lower()
    # duplicate *name* (UNIQUE constraint, different slug) -> 400 as well
    r = client.post("/api/agents/agents", json={"name": "Hermes"}, **auth)
    assert r.status_code == 400
    # missing name -> 400
    r = client.post("/api/agents/agents", json={"name": " "}, **auth)
    assert r.status_code == 400

    cslug = _uname("dup-cap")
    r = client.post("/api/agents/capabilities", json={"name": _uname("CapA"), "slug": cslug}, **auth)
    assert r.status_code == 200
    r = client.post("/api/agents/capabilities", json={"name": _uname("CapB"), "slug": cslug}, **auth)
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"].lower()
    r = client.post("/api/agents/capabilities", json={"name": "Coding"}, **auth)
    assert r.status_code == 400  # seeded capability name


def test_assessment_lifecycle_with_fresh_config_rows(client):
    """Editor + Reset flow against a just-created agent/capability."""
    admin = _login(client)
    auth = {"cookies": {"session": admin}}
    model = _uname("persist-probe")

    agent = client.post(
        "/api/agents/agents", json={"name": _uname("FreshEnv")}, **auth
    ).json()["agent"]
    cap = client.post(
        "/api/agents/capabilities", json={"name": _uname("FreshCap")}, **auth
    ).json()["capability"]

    # create
    r = client.post(
        "/api/agents/assessments",
        json={
            "model": model,
            "agent_id": agent["id"],
            "status": "good",
            "note": "verified manually — survives reload",
            "capabilities": [cap["slug"]],
        },
        **auth,
    )
    assert r.status_code == 200, r.text
    a = r.json()["assessment"]
    assert a["status"] == "good"
    assert a["agent_slug"] == agent["slug"]
    assert [c["slug"] for c in a["capabilities"]] == [cap["slug"]]
    assert a["note"] == "verified manually — survives reload"
    assert a["tested_at"] is not None

    # update (upsert on the same model×agent cell)
    r = client.post(
        "/api/agents/assessments",
        json={
            "model": model,
            "agent_id": agent["id"],
            "status": "works",
            "note": "updated note",
            "capabilities": [],
        },
        **auth,
    )
    assert r.status_code == 200
    updated = r.json()["assessment"]
    assert updated["id"] == a["id"]
    assert updated["status"] == "works"

    # reload matrix — saved state comes back (note included)
    matrix = client.get("/api/agents/matrix", **auth).json()
    rows = [x for x in matrix["assessments"] if x["model"] == model]
    assert len(rows) == 1
    assert rows[0]["status"] == "works"
    assert rows[0]["note"] == "updated note"
    assert rows[0]["agent_slug"] == agent["slug"]

    # reset -> cell back to untested (row gone)
    r = client.delete(f"/api/agents/assessments/{updated['id']}", **auth)
    assert r.status_code == 200
    assert client.delete(f"/api/agents/assessments/{updated['id']}", **auth).status_code == 404
    matrix = client.get("/api/agents/matrix", **auth).json()
    assert not any(x["model"] == model for x in matrix["assessments"])
