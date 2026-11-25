import base64

from tests.conftest import register


def basic(email: str, password: str) -> dict:
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_api_docs_are_admin_only(client):
    # Nobody logged in: the browser gets a 401 + Basic challenge, which makes it show a login box
    for path in ("/docs", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/redoc").status_code == 404  # the second built-in docs page is switched off

    register(client, email="fan@test.dev")  # attendee, password123
    assert client.get("/docs", headers=basic("fan@test.dev", "password123")).status_code == 401

    register(client, "admin", email="boss@test.dev")
    assert client.get("/docs", headers=basic("boss@test.dev", "wrong-password")).status_code == 401

    r = client.get("/docs", headers=basic("Boss@test.dev", "password123"))  # email is case-insensitive
    assert r.status_code == 200 and "swagger" in r.text.lower()
    schema = client.get("/openapi.json", headers=basic("boss@test.dev", "password123")).json()
    assert "/events" in schema["paths"]
