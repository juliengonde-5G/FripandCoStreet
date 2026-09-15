# Nouveau service (PR4, docs/ARCHITECTURE_PR4.md §3, F4) — exports bruts CSV
# (journal des ventes/journal de caisse detailles), extrait du mecanisme de
# l'application source (`services/database_backup.py::export_table_csv`) :
# liste blanche stricte, JAMAIS de table client/PII (`clients`, `consents`,
# `communications` restent hors liste — l'export client existe deja par
# fiche, cf. `GET /admin/clients/{id}/export`). Filtre `?from&to` sur
# `created_at`, UTF-8 BOM, separateur « ; », dates converties Europe/Paris.
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cash_movement import CashMovement
from app.models.jet import JournalEvent
from app.models.pos import CashDrawer, Payment, Transaction, TransactionItem, ZReport

_PARIS = ZoneInfo("Europe/Paris")

# Liste blanche stricte (F4) — journal des ventes / journal de caisse
# uniquement. `clients`/`consents`/`communications` sont volontairement
# ABSENTS (PII, RGPD).
_TABLE_MODELS: dict[str, type] = {
    "transactions": Transaction,
    "transaction_items": TransactionItem,
    "payments": Payment,
    "z_reports": ZReport,
    "cash_movements": CashMovement,
    "cash_drawers": CashDrawer,
    "journal_events": JournalEvent,
}

EXPORTABLE_TABLES = sorted(_TABLE_MODELS.keys())


class UnknownTableError(ValueError):
    """Table absente de la liste blanche `EXPORTABLE_TABLES`."""


def _csv_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_PARIS).strftime("%d/%m/%Y %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if hasattr(value, "value"):  # Enum
        return str(value.value)
    return str(value)


async def export_table_csv(
    db: AsyncSession,
    table_key: str,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[str, str]:
    """Exporte une table de la liste blanche en CSV (BOM UTF-8, `;`).

    Filtre optionnel sur `created_at` (`date_from`/`date_to`, bornes
    incluses). Retourne ``(filename, csv_text)`` — le texte commence par le
    caractere BOM U+FEFF (encode en 3 octets EF BB BF une fois servi en
    UTF-8), pour qu'Excel detecte correctement l'encodage."""
    if table_key not in _TABLE_MODELS:
        raise UnknownTableError(f"Table non exportable : {table_key}")

    model = _TABLE_MODELS[table_key]
    table: Table = model.__table__
    columns = [c.name for c in table.columns]

    query = table.select()
    if "created_at" in columns:
        if date_from is not None:
            query = query.where(table.c.created_at >= date_from)
        if date_to is not None:
            query = query.where(table.c.created_at <= date_to)
        query = query.order_by(table.c.created_at.asc())

    result = await db.execute(query)

    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(columns)
    for row in result:
        m = row._mapping
        writer.writerow([_csv_value(m[c]) for c in columns])

    now_paris = datetime.now(timezone.utc).astimezone(_PARIS)
    stamp = now_paris.strftime("%Y%m%d")
    return f"fripco_{table_key}_{stamp}.csv", buf.getvalue()
