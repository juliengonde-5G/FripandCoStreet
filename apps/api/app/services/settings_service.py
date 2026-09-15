# Nouveau service — remplace le fichier `data/app_config.json` de l'application source par
# un stockage en base (`app_settings`, D13 du contrat PR2). Modelise sur le
# JournalService (verrou avisory Postgres avant lecture, ecriture JET dans la
# meme transaction SQL, jamais d'exception avalee).
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import AppSetting
from app.services.jet import EVENT_CONFIG_CHANGED, JournalService
from app.services.tva_service import DEFAULT_TVA_RATE

# Cle du verrou avisory Postgres dediee aux parametres boutique — distincte
# de celle du JET (837_120_001) et de celle de la caisse (5_252_026), pour ne
# jamais serialiser des operations independantes entre elles.
_SETTINGS_ADVISORY_LOCK_KEY = 837_120_002

DEFAULT_VALUES: dict[str, dict[str, Any]] = {
    "shop": {
        "name": "Frip & Co Street",
        "address_line1": "",
        "address_line2": "",
        "postal_code": "",
        "city": "",
        "siret": "",
        "vat_number": "",
        "phone": "",
        "email": "",
        # PR3 (E8) — déclaré par `ShopSettingsIn` (app/api/admin/router.py) ;
        # doit être renvoyé par défaut (avant tout PUT) comme les autres
        # champs, pas seulement une fois la clé écrite au moins une fois.
        "dpo_email": "",
    },
    "fiscal": {"tva_rate": f"{DEFAULT_TVA_RATE:.2f}"},
    "receipt": {"header_note": "", "footer_note": "", "return_policy": ""},
    # PR3b (impression tickets, décision Julien) — matériel MUNBYN 047P +
    # tiroir Safescan SD-4141 : jamais de secret ici, uniquement de la
    # config réseau/USB. Voir `app/api/admin/router.py::HardwareSettingsIn`
    # et `app/api/pos/router.py` / `app/api/hardware/router.py`.
    "hardware": {
        "printer_mode": "none",
        "printer_host": "",
        "printer_port": 9100,
        "drawer_enabled": False,
        "drawer_pin": 0,
        "auto_print_on_sale": False,
        "auto_kick_on_cash": False,
    },
}


class SettingsService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, key: str) -> dict[str, Any]:
        """Retourne la valeur courante d'une cle, ou son defaut si absente."""
        row = (
            await self.db.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalar_one_or_none()
        if row is None:
            return dict(DEFAULT_VALUES.get(key, {}))
        return dict(row.value or {})

    async def get_tva_rate(self) -> Decimal:
        """Taux de TVA courant (D8) — utilise a la vente, fige par ligne."""
        fiscal = await self.get("fiscal")
        try:
            return Decimal(str(fiscal.get("tva_rate", DEFAULT_TVA_RATE)))
        except Exception:
            return DEFAULT_TVA_RATE

    async def set(
        self,
        key: str,
        value: dict[str, Any],
        *,
        user_id: uuid.UUID | None,
    ) -> AppSetting:
        """Ecrit une cle de parametrage et journalise le changement au JET.

        Le verrou avisory serialise lecture-avant-ecriture + calcul du diff :
        deux managers modifiant la meme cle en meme temps ne doivent pas
        produire un diff JET incoherent avec ce qui a ete réellement écrasé.
        """
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _SETTINGS_ADVISORY_LOCK_KEY},
        )

        row = (
            await self.db.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalar_one_or_none()
        previous = dict(row.value or {}) if row is not None else dict(
            DEFAULT_VALUES.get(key, {})
        )

        if row is None:
            row = AppSetting(key=key, value=value, updated_by_user_id=user_id)
            self.db.add(row)
        else:
            row.value = value
            row.updated_by_user_id = user_id

        diff = {
            "before": previous,
            "after": value,
            "changed_fields": sorted(
                k for k in {*previous.keys(), *value.keys()} if previous.get(k) != value.get(k)
            ),
        }
        await JournalService(self.db).record(
            EVENT_CONFIG_CHANGED,
            user_id=user_id,
            payload={"key": key, **diff},
        )
        await self.db.flush()
        return row
