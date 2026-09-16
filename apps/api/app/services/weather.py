# Nouveau service (PR11, M3 de docs/ARCHITECTURE_PR11.md) — meteo locale
# affichee a cote du chiffre du jour sur l'accueil et figee dans le cahier
# du jour (M2).
#
# Trois principes, dans l'ordre d'importance :
#
# 1. La meteo ne doit JAMAIS casser une page. C'est un agrement, pas une
#    donnee metier : cle absente, ville absente, OpenWeather en panne,
#    reponse illisible -> `{"unavailable": True, "reason": …}`. Aucune
#    exception ne remonte a l'appelant, jamais.
# 2. La cle d'API n'apparait nulle part ailleurs que dans la requete
#    sortante : ni dans les reglages (`app_settings`), ni dans une reponse
#    d'API, ni dans un log — les URL journalisees sont volontairement
#    reconstruites sans la query string.
# 3. Une seule requete sortante a la fois, et au plus une par quart d'heure.
#    L'accueil, le cahier et la route dediee tapent la meme donnee ; sans
#    cache la boutique consommerait son quota OpenWeather pour rien. Pas de
#    Redis dans cette application (contrat PR11) : le cache est en memoire
#    de processus, protege par un verrou asyncio.
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.settings_service import SettingsService

_log = logging.getLogger("fripco.weather")

OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
REQUEST_TIMEOUT = 8.0
CACHE_TTL_SECONDS = 15 * 60

# Raisons d'indisponibilite — courtes, en francais, affichables telles
# quelles par le front (« Meteo indisponible — <raison> »).
REASON_NO_KEY = "clé OpenWeather non configurée"
REASON_NO_CITY = "ville non renseignée dans les réglages"
REASON_UNREACHABLE = "service météo injoignable"
REASON_BAD_RESPONSE = "réponse météo illisible"

# Cache de processus : (cle_de_cache, instant_monotone, charge_utile).
# `_LOCK` serialise les appels sortants — dix onglets ouverts sur l'accueil
# declenchent une requete, pas dix.
_LOCK = asyncio.Lock()
_CACHE: tuple[tuple[Any, ...], float, dict[str, Any]] | None = None

# Transport httpx injecte par les tests (faux OpenWeather local). En
# production il reste a None : httpx utilise son transport reseau normal.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Injecte (ou retire) le transport httpx — tests uniquement."""
    global _TRANSPORT
    _TRANSPORT = transport
    reset_cache()


def reset_cache() -> None:
    """Vide le cache memoire (tests, ou changement de reglage meteo)."""
    global _CACHE
    _CACHE = None


def _unavailable(reason: str) -> dict[str, Any]:
    return {"unavailable": True, "reason": reason}


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # NaN -> None


async def resolve_location(db: AsyncSession) -> dict[str, Any]:
    """Ville / coordonnees a interroger : reglage `weather`, repli `shop.city`.

    Aucun geocodage (hors perimetre, §2 du contrat) : sans latitude ni
    longitude, la ville est passee telle quelle a OpenWeather via `q=`.
    """
    service = SettingsService(db)
    weather = await service.get("weather")
    city = str(weather.get("city") or "").strip()
    if not city:
        shop = await service.get("shop")
        city = str(shop.get("city") or "").strip()
    return {
        "city": city,
        "lat": _as_float(weather.get("lat")),
        "lon": _as_float(weather.get("lon")),
    }


def _params(location: dict[str, Any], api_key: str) -> dict[str, str]:
    params = {"units": "metric", "lang": "fr", "appid": api_key}
    lat, lon = location["lat"], location["lon"]
    if lat is not None and lon is not None:
        params["lat"] = f"{lat}"
        params["lon"] = f"{lon}"
    else:
        params["q"] = location["city"]
    return params


def _map_payload(raw: Any, location: dict[str, Any], fetched_at: float) -> dict[str, Any]:
    """Traduit la reponse OpenWeather en la forme du contrat (M3)."""
    if not isinstance(raw, dict):
        return _unavailable(REASON_BAD_RESPONSE)
    main = raw.get("main") if isinstance(raw.get("main"), dict) else {}
    weather_list = raw.get("weather") if isinstance(raw.get("weather"), list) else []
    first = weather_list[0] if weather_list and isinstance(weather_list[0], dict) else {}
    wind = raw.get("wind") if isinstance(raw.get("wind"), dict) else {}

    temp = _as_float(main.get("temp"))
    if temp is None:
        # Sans temperature, il n'y a rien a afficher : autant l'annoncer
        # indisponible plutot que de peindre une carte vide.
        return _unavailable(REASON_BAD_RESPONSE)

    description = str(first.get("description") or "").strip()
    return {
        "unavailable": False,
        "description": description,
        "temp": round(temp, 1),
        "temp_min": round(_as_float(main.get("temp_min")) or temp, 1),
        "temp_max": round(_as_float(main.get("temp_max")) or temp, 1),
        "icon": str(first.get("icon") or "").strip(),
        "wind_speed": round(_as_float(wind.get("speed")) or 0.0, 1),
        "city": str(raw.get("name") or location["city"] or "").strip(),
        "fetched_at": _iso(fetched_at),
    }


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


async def _fetch(location: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Un aller-retour OpenWeather, sans jamais laisser filtrer la cle.

    Les erreurs httpx portent l'URL complete (donc `appid=…`) : on ne
    journalise donc que le type d'erreur, jamais `str(exc)` ni la requete.
    """
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, transport=_TRANSPORT
        ) as http_client:
            response = await http_client.get(OPENWEATHER_URL, params=_params(location, api_key))
    except Exception as exc:  # noqa: BLE001 — la meteo n'echoue jamais bruyamment
        _log.warning("Appel météo échoué (%s)", type(exc).__name__)
        return _unavailable(REASON_UNREACHABLE)

    if response.status_code != 200:
        # Le corps d'erreur OpenWeather ne contient pas la cle, mais il peut
        # contenir la ville : on ne garde que le code de statut.
        _log.warning("Appel météo refusé (HTTP %s)", response.status_code)
        return _unavailable(REASON_UNREACHABLE)

    try:
        raw = response.json()
    except Exception:  # noqa: BLE001
        return _unavailable(REASON_BAD_RESPONSE)

    return _map_payload(raw, location, time.time())


async def get_current(db: AsyncSession) -> dict[str, Any]:
    """Meteo courante de la boutique, ou `{"unavailable": True, "reason": …}`.

    Ne leve jamais. Le resultat est cache 15 minutes, y compris une
    indisponibilite reseau : marteler un service en panne ne le repare pas,
    et l'utilisateur prefere une carte « indisponible » immediate a huit
    secondes d'attente a chaque rafraichissement. En revanche une cle ou
    une ville absentes ne sont PAS cachees : elles se reglent dans l'admin,
    et le resultat doit changer des le rechargement de la page.
    """
    api_key = (settings.OPENWEATHER_API_KEY or "").strip()
    if not api_key:
        return _unavailable(REASON_NO_KEY)

    try:
        location = await resolve_location(db)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Lecture du réglage météo échouée (%s)", type(exc).__name__)
        return _unavailable(REASON_UNREACHABLE)

    if location["city"] == "" and (location["lat"] is None or location["lon"] is None):
        return _unavailable(REASON_NO_CITY)

    # La cle fait partie de l'identite du cache (sans jamais y etre stockee
    # en clair) : changer de compte OpenWeather doit invalider le cache.
    cache_key = (hash(api_key), location["city"], location["lat"], location["lon"])

    global _CACHE
    cached = _CACHE
    now = time.monotonic()
    if cached is not None and cached[0] == cache_key and now - cached[1] < CACHE_TTL_SECONDS:
        return dict(cached[2])

    async with _LOCK:
        # Re-verification sous verrou : pendant l'attente, un autre appel a
        # pu remplir le cache — inutile de refaire la requete.
        cached = _CACHE
        now = time.monotonic()
        if cached is not None and cached[0] == cache_key and now - cached[1] < CACHE_TTL_SECONDS:
            return dict(cached[2])

        payload = await _fetch(location, api_key)
        _CACHE = (cache_key, time.monotonic(), payload)
        return dict(payload)


def api_key_configured() -> bool:
    """Etat de la cle pour l'ecran de reglages — jamais sa valeur (M3)."""
    return bool((settings.OPENWEATHER_API_KEY or "").strip())


def cache_age_seconds() -> int | None:
    """Age du cache meteo en secondes, `None` s'il est vide (PR12, N2).

    Sert uniquement a la supervision : un cache qui ne vieillit jamais
    trahit un service meteo qui ne repond plus, sans avoir a declencher une
    requete sortante depuis l'ecran de supervision."""
    cached = _CACHE
    if cached is None:
        return None
    return max(int(time.monotonic() - cached[1]), 0)
