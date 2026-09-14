# Nouveau test (passe d'integration PR2) : pagination du JET
# (`GET /admin/jet?limit=&before_seq=`) — `next_before_seq` porte le curseur
# pour le front, `null` quand il n'y a plus rien au-dela de la page.
import pytest

pytestmark = pytest.mark.anyio


async def test_jet_listing_next_before_seq_null_when_page_not_full(client, auth_headers):
    # Le login (auth_headers) a deja ecrit au moins un evenement JET.
    r = await client.get("/api/admin/jet", params={"limit": 100}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["events"])
    assert body["next_before_seq"] is None


async def test_jet_listing_next_before_seq_paginates_full_pages(client, auth_headers):
    # `auth_headers` a deja produit >= 1 evenement (auth.login_success) ;
    # on en genere assez pour depasser une petite page.
    for _ in range(4):
        await client.post("/api/pos/drawer/open", json={"opening_amount": "0"}, headers=auth_headers)
        await client.post("/api/pos/drawer/close", json={"closing_amount": "0"}, headers=auth_headers)

    r = await client.get("/api/admin/jet", params={"limit": 3}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["events"]) == 3
    assert body["next_before_seq"] == body["events"][-1]["seq"]

    r2 = await client.get(
        "/api/admin/jet", params={"limit": 3, "before_seq": body["next_before_seq"]}, headers=auth_headers
    )
    body2 = r2.json()
    # Toute la deuxieme page a un seq strictement inferieur au curseur.
    assert all(e["seq"] < body["next_before_seq"] for e in body2["events"])
