from datetime import UTC, datetime, timedelta

from app.db import SessionLocal
from app.seed import DEMO_USER, DHAKA_CATALOG, seed
from tests.conftest import register


def _create(client, headers, title, category="music", area="Gulshan", days=5):
    starts = (datetime.now(UTC) + timedelta(days=days)).isoformat()
    r = client.post("/events", headers=headers, json={
        "title": title, "venue": "Some Hall", "area": area, "category": category,
        "starts_at": starts, "rows": 1, "seats_per_row": 2, "price_cents": 30000,
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_event_has_category_area_and_price_from(client, organizer):
    ev = _create(client, organizer, "Rooftop Jam")
    assert ev["category"] == "music" and ev["area"] == "Gulshan" and ev["price_from_cents"] == 30000
    item = client.get("/events").json()["items"][0]
    assert item["category"] == "music" and item["price_from_cents"] == 30000


def test_invalid_category_rejected(client, organizer):
    r = client.post("/events", headers=organizer, json={
        "title": "X", "venue": "Y", "category": "nightclub", "starts_at": "2030-01-01T00:00:00Z",
        "rows": 1, "seats_per_row": 1, "price_cents": 0,
    })
    assert r.status_code == 422


def test_filters_search_and_past_events(client, organizer):
    _create(client, organizer, "Jazz Evening", "music", "Uttara")
    _create(client, organizer, "Football Cup", "sports", "Mirpur")
    _create(client, organizer, "Old Concert", "music", "Gulshan", days=-3)  # already happened

    titles = lambda **p: {e["title"] for e in client.get("/events", params=p).json()["items"]}  # noqa: E731
    assert titles() == {"Jazz Evening", "Football Cup"}                     # past events hidden
    assert titles(include_past=True) == {"Jazz Evening", "Football Cup", "Old Concert"}
    assert titles(category="sports") == {"Football Cup"}
    assert titles(area="uttara") == {"Jazz Evening"}                         # case-insensitive
    assert titles(q="foot") == {"Football Cup"}
    assert titles(q="mirpur") == {"Football Cup"}                            # search covers area too
    assert client.get("/events", params={"category": "nightclub"}).status_code == 422

    meta = client.get("/events/meta").json()
    assert "sports" in meta["categories"] and set(meta["areas"]) == {"Uttara", "Mirpur"}


def test_cache_respects_filters(client, organizer):
    _create(client, organizer, "A", "music")
    client.get("/events", params={"category": "music"})  # warm the cache
    _create(client, organizer, "B", "music")              # must invalidate it
    assert client.get("/events", params={"category": "music"}).json()["total"] == 2


def test_seed_loads_catalog_once_and_demo_user(client):
    with SessionLocal() as db:
        seed(db)
        seed(db)  # idempotent
    page = client.get("/events", params={"limit": 100}).json()
    assert page["total"] == len(DHAKA_CATALOG)
    assert len({e["category"] for e in page["items"]}) >= 6
    r = client.post("/auth/login", json={"email": DEMO_USER[0], "password": DEMO_USER[2]})
    assert r.status_code == 200


def test_admin_load_demo_events_skips_existing(client):
    admin = register(client, "admin")
    first = client.post("/admin/demo-events", headers=admin).json()["created"]
    second = client.post("/admin/demo-events", headers=admin).json()["created"]
    assert first == len(DHAKA_CATALOG) and second == 0
    assert client.post("/admin/demo-events", headers=register(client)).status_code == 403
