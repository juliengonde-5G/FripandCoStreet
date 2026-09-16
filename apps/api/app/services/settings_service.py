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
    # PR4 (F1, docs/ARCHITECTURE_PR4.md §1/§3) — plan de comptes comptable,
    # defauts IDENTIQUES a l'application source (`AccountingConfig`) : pas
    # d'integration Pennylane (decision Julien, §1 du contrat — l'import se
    # fait par fichier CSV/FEC). Voir `app/services/accounting_service.py`.
    "accounting": {
        "journal_code": "VTE",
        "account_sales": "707100",
        "label_sales": "Ventes marchandises",
        "account_tva": "44571",
        "label_tva": "TVA collectée 20%",
        "account_cash": "531000",
        "label_cash": "Caisse",
        "account_card": "512000",
        "label_card": "CB SumUp",
        "account_rounding_expense": "658000",
        "account_rounding_income": "758000",
    },
    # PR5 (G3, docs/ARCHITECTURE_PR5.md §1) — sauvegarde applicative de la
    # base : retention en jours, activation du cron nocturne 03:00, e-mail
    # d'alerte (vide -> repli sur `shop.email`, voir
    # `app/jobs.py::run_nightly_database_backup`). Aucun secret ici — le
    # dossier des dumps (`BACKUP_DIR`) est une variable d'environnement
    # (app/core/config.py), pas un reglage boutique.
    "backup": {
        "retention_days": 60,
        "nightly_enabled": True,
        "alert_email": "",
    },
    # PR6 (H1, docs/ARCHITECTURE_PR6.md §1) — objectifs de chiffre d'affaires
    # du tableau de bord d'accueil. Montants en euros TTC **nets** (ventes
    # moins annulations), stockes en chaines a 2 decimales comme
    # `fiscal.tva_rate` (JSONB ne sait pas porter un Decimal). Aucun impact
    # fiscal : lecture seule des ventes, rien n'est signe ici.
    #   daily   : objectif par jour ouvert ("0.00" = pas d'objectif).
    #   monthly : carte "YYYY-MM" -> objectif ; un mois absent herite de
    #             monthly["default"] s'il existe, sinon 0.
    "targets": {"daily": "0.00", "monthly": {}},
    # PR8 (J2, docs/ARCHITECTURE_PR8.md) — identification des vendeuses en
    # caisse. `cashier_required` a **false** par defaut : la boutique tourne
    # exactement comme avant PR8 tant que le manager n'a pas cree ses
    # vendeuses. Une fois vrai, aucune vente, annulation ni mouvement de
    # caisse n'est possible sans vendeuse identifiee (422 `cashier_required`).
    "pos": {"cashier_required": False},
    # PR9 (K1, docs/ARCHITECTURE_PR9.md) — retention du journal des echanges
    # SumUp (`sumup_exchanges`), purge par le cron nocturne de 03:00 apres
    # la sauvegarde. Bornes 7-730 jours : en dessous d'une semaine on ne
    # peut plus deboguer un incident du week-end, au-dela de deux ans la
    # table grossit sans servir a personne. Aucun impact fiscal — table
    # d'exploitation, aucune vente n'y nait.
    "payments": {"exchange_retention_days": 90},
    # PR10 (L5, docs/ARCHITECTURE_PR10.md) — fenetre de reflexion avant
    # l'effacement d'une fiche cliente. Une suppression RGPD n'est plus
    # immediate : la demande est enregistree, la cliente en est informee par
    # e-mail, et un cron anonymise a echeance. Le delai est reglable parce
    # que la boutique doit pouvoir le raccourcir si la cliente insiste, ou
    # l'allonger le temps d'un litige — bornes 1-90 jours : en dessous d'un
    # jour la demande ne serait plus annulable (c'est tout l'interet du
    # differe), au-dela de trois mois on ne « differe » plus, on enterre.
    "rgpd": {"deletion_delay_days": 30},
    # PR11 (M3, docs/ARCHITECTURE_PR11.md) — meteo locale affichee a cote du
    # chiffre du jour. Ville vide -> repli sur `shop.city`. Latitude et
    # longitude facultatives : renseignees, elles priment sur la ville (deux
    # communes homonymes ne se departagent pas autrement) ; absentes, la
    # ville est passee telle quelle a OpenWeather (pas de geocodage, §2 du
    # contrat). La CLE d'API n'est PAS ici : c'est un secret, donc une
    # variable d'environnement (`OPENWEATHER_API_KEY`), jamais un reglage.
    "weather": {"city": "", "lat": None, "lon": None},
}

# Bornes du delai de suppression RGPD (L5). Le clamp vit ici, a cote du
# defaut : le cron et le service doivent tourner meme si la valeur a ete
# ecrite a la main dans le JSONB avec une valeur aberrante.
DELETION_DELAY_MIN_DAYS = 1
DELETION_DELAY_MAX_DAYS = 90
DELETION_DELAY_DEFAULT_DAYS = 30


def clamp_deletion_delay_days(value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DELETION_DELAY_DEFAULT_DAYS
    return max(DELETION_DELAY_MIN_DAYS, min(days, DELETION_DELAY_MAX_DAYS))


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

    async def get_cashier_required(self) -> bool:
        """Reglage `pos.cashier_required` (PR8/J2) — booleen tolerant aux
        valeurs heritees d'un JSONB ecrit a la main (`"true"`, `1`...)."""
        pos = await self.get("pos")
        value = pos.get("cashier_required", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    async def get_exchange_retention_days(self) -> int:
        """Reglage `payments.exchange_retention_days` (PR9/K1), borne 7-730.

        Passe par le clamp du service de journal plutot que de faire
        confiance au JSONB : la purge nocturne doit tourner meme si le
        reglage a ete ecrit a la main avec une valeur aberrante.
        """
        from app.services.sumup_exchange_log import clamp_retention_days

        payments = await self.get("payments")
        return clamp_retention_days(payments.get("exchange_retention_days"))

    async def get_deletion_delay_days(self) -> int:
        """Reglage `rgpd.deletion_delay_days` (PR10/L5), borne 1-90 jours."""
        rgpd = await self.get("rgpd")
        return clamp_deletion_delay_days(rgpd.get("deletion_delay_days"))

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
