# Extrait de l'application source (apps/api/app/core/logging_config.py)
"""Configuration de logging centralisee pour l'API fripco-street.

Utiliser `setup_logging()` une fois au demarrage. Formatteur JSON disponible
pour l'expedition de logs en production (Loki, Datadog, ELK) via `LOG_JSON=true`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from app.core.config import settings


class JsonFormatter(logging.Formatter):
    """Formatteur JSON structure minimal — sans dependance tierce."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "user_id", "path", "method", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """Configure le logger racine et le logger 'fripco'.

    Idempotent — peut etre appelee plusieurs fois sans effet de bord.
    """
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    if settings.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    # httpx journalise CHAQUE requete sortante en INFO, URL complete
    # comprise : « HTTP Request: GET https://…?appid=… "HTTP/1.1 200 OK" ».
    # Nos autres appels sortants portent leur secret dans un en-tete
    # (SumUp, Brevo : `Authorization` / `api-key`), qui n'apparait pas dans
    # cette ligne — mais OpenWeather (PR11, M3) exige sa cle en QUERY
    # STRING : la laisser a INFO ecrirait la cle en clair dans les logs de
    # production, et donc dans le collecteur qui les recoit. On coupe la
    # source plutot que de filtrer apres coup : un filtre se contourne au
    # premier logger tiers ajoute, un niveau non. `httpcore` (la couche en
    # dessous) trace les memes URL en DEBUG — meme traitement.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    fripco = logging.getLogger("fripco")
    fripco.setLevel(level)
    fripco.propagate = True
