# Nouveau test (PR11, §3 de docs/ARCHITECTURE_PR11.md) — service
# `weather.py` (M3), reglage `weather` et route `GET /api/reports/weather`.
#
# Aucun appel reseau reel : un `httpx.MockTransport` est injecte dans le
# service (`set_transport`), ce qui exerce le VRAI chemin httpx (URL,
# parametres, timeout, decodage JSON) sans sortir du conteneur.
import logging

import httpx
import pytest

from app.core.config import settings
from app.core.database import async_session
from app.core.logging_config import setup_logging
from app.services import weather as weather_service
from app.services.settings_service import SettingsService

pytestmark = pytest.mark.anyio

OPENWEATHER_SAMPLE = {
    "name": "Ville d'essai",
    "weather": [{"description": "légère pluie", "icon": "10d"}],
    "main": {"temp": 14.37, "temp_min": 12.1, "temp_max": 16.8},
    "wind": {"speed": 4.62},
}


@pytest.fixture(autouse=True)
def _clean_weather_state(monkeypatch):
    """Chaque test repart d'un cache vide et sans transport injecte.

    Le cache est en memoire de PROCESSUS (pas de Redis dans cette
    application) : sans ce nettoyage, la meteo d'un test fuiterait dans le
    suivant.
    """
    weather_service.set_transport(None)
    yield
    weather_service.set_transport(None)


class _Recorder:
    """Faux OpenWeather : compte les appels et retient la derniere requete."""

    def __init__(self, payload=None, status: int = 200, boom: bool = False):
        self.payload = OPENWEATHER_SAMPLE if payload is None else payload
        self.status = status
        self.boom = boom
        self.calls = 0
        self.last_request: httpx.Request | None = None

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            self.last_request = request
            if self.boom:
                raise httpx.ConnectError("réseau coupé", request=request)
            return httpx.Response(self.status, json=self.payload)

        return httpx.MockTransport(handler)


def _install(monkeypatch, recorder: _Recorder, *, api_key: str = "cle-de-test") -> None:
    monkeypatch.setattr(settings, "OPENWEATHER_API_KEY", api_key)
    weather_service.set_transport(recorder.transport())


async def _set_weather_setting(city: str = "", **extra) -> None:
    async with async_session() as db:
        await SettingsService(db).set(
            "weather", {"city": city, "lat": None, "lon": None, **extra}, user_id=None
        )
        await db.commit()
    weather_service.reset_cache()


async def _current() -> dict:
    async with async_session() as db:
        return await weather_service.get_current(db)


# ---------------------------------------------------------------------------
# Chemin nominal
# ---------------------------------------------------------------------------


async def test_openweather_payload_is_mapped_to_the_contract(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")

    data = await _current()

    assert data["unavailable"] is False
    assert data["description"] == "légère pluie"
    assert data["temp"] == 14.4
    assert data["temp_min"] == 12.1
    assert data["temp_max"] == 16.8
    assert data["icon"] == "10d"
    assert data["wind_speed"] == 4.6
    assert data["city"] == "Ville d'essai"
    assert data["fetched_at"]

    params = recorder.last_request.url.params
    assert params["units"] == "metric"
    assert params["lang"] == "fr"
    assert params["q"] == "Ville d'essai"


async def test_coordinates_take_precedence_over_the_city(monkeypatch):
    """Deux communes homonymes ne se departagent pas autrement."""
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai", lat=49.1, lon=1.5)

    await _current()

    params = recorder.last_request.url.params
    assert params["lat"] == "49.1"
    assert params["lon"] == "1.5"
    assert "q" not in params


async def test_city_falls_back_to_the_shop_address(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    async with async_session() as db:
        service = SettingsService(db)
        shop = await service.get("shop")
        await service.set("shop", {**shop, "city": "Ville de la boutique"}, user_id=None)
        await db.commit()
    await _set_weather_setting("")

    await _current()
    assert recorder.last_request.url.params["q"] == "Ville de la boutique"


# ---------------------------------------------------------------------------
# Indisponibilites — jamais une exception
# ---------------------------------------------------------------------------


async def test_missing_api_key_is_unavailable_without_any_call(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder, api_key="")
    await _set_weather_setting("Ville d'essai")

    data = await _current()

    assert data == {"unavailable": True, "reason": weather_service.REASON_NO_KEY}
    assert recorder.calls == 0


async def test_missing_city_is_unavailable_without_any_call(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    async with async_session() as db:
        service = SettingsService(db)
        shop = await service.get("shop")
        await service.set("shop", {**shop, "city": ""}, user_id=None)
        await db.commit()
    await _set_weather_setting("")

    data = await _current()

    assert data["unavailable"] is True
    assert data["reason"] == weather_service.REASON_NO_CITY
    assert recorder.calls == 0


async def test_network_error_is_unavailable_and_never_raises(monkeypatch):
    recorder = _Recorder(boom=True)
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")

    data = await _current()

    assert data == {"unavailable": True, "reason": weather_service.REASON_UNREACHABLE}
    assert recorder.calls == 1


async def test_http_error_is_unavailable(monkeypatch):
    recorder = _Recorder(payload={"message": "Invalid API key"}, status=401)
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")

    assert (await _current())["reason"] == weather_service.REASON_UNREACHABLE


async def test_unreadable_payload_is_unavailable(monkeypatch):
    recorder = _Recorder(payload={"cod": 200})
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")

    assert (await _current())["reason"] == weather_service.REASON_BAD_RESPONSE


# ---------------------------------------------------------------------------
# Cache et secret
# ---------------------------------------------------------------------------


async def test_two_reads_within_the_window_make_one_outgoing_call(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")

    first = await _current()
    second = await _current()

    assert recorder.calls == 1
    assert first == second


async def test_changing_the_city_invalidates_the_cache(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")
    await _current()

    await _set_weather_setting("Autre ville")
    await _current()

    assert recorder.calls == 2


async def test_expired_cache_is_refetched(monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")
    await _current()

    # On vieillit le cache de plus d'un quart d'heure sans attendre.
    key, _stamp, payload = weather_service._CACHE
    weather_service._CACHE = (key, _stamp - weather_service.CACHE_TTL_SECONDS - 1, payload)
    await _current()

    assert recorder.calls == 2


async def test_the_api_key_never_reaches_the_logs(monkeypatch, caplog):
    secret = "cle-ultra-secrete-a-ne-jamais-journaliser"
    recorder = _Recorder(boom=True)
    _install(monkeypatch, recorder, api_key=secret)
    await _set_weather_setting("Ville d'essai")

    with caplog.at_level(logging.DEBUG):
        data = await _current()

    assert data["unavailable"] is True
    assert secret not in caplog.text
    assert "appid" not in caplog.text.lower()


async def test_a_successful_call_logs_no_url_at_all(monkeypatch, caplog):
    """Le chemin NOMINAL est le vrai danger : httpx journalise chaque
    requete sortante en INFO, URL complete comprise. OpenWeather exige sa
    cle en query string (contrairement a SumUp/Brevo, qui la mettent dans
    un en-tete) : cette ligne ecrirait donc la cle en clair dans les logs
    de production. `setup_logging` coupe la source (`httpx`/`httpcore` a
    WARNING) — ce test echoue si quelqu'un l'y remet un jour.
    """
    setup_logging()
    secret = "cle-ultra-secrete-a-ne-jamais-journaliser"
    recorder = _Recorder()
    _install(monkeypatch, recorder, api_key=secret)
    await _set_weather_setting("Ville d'essai")

    with caplog.at_level(logging.INFO):
        data = await _current()

    assert data["unavailable"] is False
    assert recorder.calls == 1
    assert secret not in caplog.text
    assert "appid" not in caplog.text.lower()
    assert "openweathermap" not in caplog.text.lower()


def test_setup_logging_muzzles_the_outgoing_http_loggers():
    """Le reglage doit etre APPLIQUE, pas seulement ecrit : `setup_logging`
    est appelee au chargement de `app.main` (donc au demarrage de l'API et,
    ici, a l'import du client de test)."""
    import app.main  # noqa: F401 — l'import est justement ce qu'on teste

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ---------------------------------------------------------------------------
# Reglage et route
# ---------------------------------------------------------------------------


async def test_settings_expose_the_key_state_but_never_its_value(
    client, auth_headers, monkeypatch
):
    monkeypatch.setattr(settings, "OPENWEATHER_API_KEY", "cle-de-test")

    r = await client.get("/api/admin/settings/weather", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"city": "", "lat": None, "lon": None, "api_key_configured": True}

    r = await client.put(
        "/api/admin/settings/weather",
        json={"city": "Ville d'essai", "lat": 49.1, "lon": 1.5},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["city"] == "Ville d'essai"
    assert body["api_key_configured"] is True
    # La cle n'est ni renvoyee, ni stockee : `app_settings` ne doit porter
    # que la localisation.
    assert "cle-de-test" not in r.text
    async with async_session() as db:
        stored = await SettingsService(db).get("weather")
    assert set(stored) == {"city", "lat", "lon"}


async def test_settings_without_a_key_report_it_as_absent(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "OPENWEATHER_API_KEY", "")
    r = await client.get("/api/admin/settings/weather", headers=auth_headers)
    assert r.json()["api_key_configured"] is False


async def test_route_returns_200_even_when_unavailable(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "OPENWEATHER_API_KEY", "")

    r = await client.get("/api/reports/weather", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["unavailable"] is True


async def test_route_requires_authentication(client):
    assert (await client.get("/api/reports/weather")).status_code == 401


async def test_route_serves_the_mapped_payload(client, auth_headers, monkeypatch):
    recorder = _Recorder()
    _install(monkeypatch, recorder)
    await _set_weather_setting("Ville d'essai")

    r = await client.get("/api/reports/weather", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "légère pluie"
    assert r.json()["temp"] == 14.4
