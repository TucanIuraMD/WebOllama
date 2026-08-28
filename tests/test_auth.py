"""Tests for authentication: password hashing, sessions, login flow."""
import asyncio

import pytest

from webui.auth import AuthManager, hash_password, verify_password


def test_password_hash_roundtrip():
    h = hash_password("secret123")
    assert h.startswith("pbkdf2$")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


@pytest.mark.asyncio
async def test_auth_manager_flow(auth_manager):
    ok, msg = await auth_manager.create_user("bob", "testpass", role="admin")
    assert ok, msg
    user = await auth_manager.authenticate("bob", "testpass")
    assert user is not None
    assert user["username"] == "bob"
    assert await auth_manager.authenticate("bob", "nope") is None

    token = await auth_manager.create_session(user)
    got = await auth_manager.get_user_from_token(token)
    assert got["username"] == "bob"

    await auth_manager.logout(token)
    assert await auth_manager.get_user_from_token(token) is None


@pytest.mark.asyncio
async def test_change_password(auth_manager):
    ok, msg = await auth_manager.create_user("carol", "oldpass1", role="admin")
    assert ok, msg
    user = await auth_manager.authenticate("carol", "oldpass1")
    assert user is not None
    ok, msg = await auth_manager.change_password(user["id"], "oldpass1", "newpass12")
    assert ok, msg
    assert await auth_manager.authenticate("carol", "newpass12") is not None


@pytest.mark.asyncio
async def test_login_endpoint(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_login_bad_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_requires_auth(client):
    assert client.get("/api/ollama/models").status_code == 401
