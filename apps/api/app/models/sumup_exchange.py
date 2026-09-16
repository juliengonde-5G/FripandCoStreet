# Nouveau modele (PR9, docs/ARCHITECTURE_PR9.md, contrat K1) — journal des
# echanges HTTP sortants avec SumUp, pour le debogage carte.
#
# Table d'EXPLOITATION, hors perimetre fiscal : aucune vente n'y nait, donc
# aucun trigger d'immuabilite et une purge assumee (retention reglable
# `payments.exchange_retention_days`, purge nocturne + bouton admin).
#
# Contenu : JAMAIS de donnee personnelle (ni nom ni e-mail), JAMAIS la cle
# API ni l'en-tete Authorization, JAMAIS de PAN. Les payloads sont rediges
# par `redact_sumup_error` avant d'arriver ici, et la reponse est tronquee
# a 4 Ko. Le service `SumUpService` ne connait pas la base : il empile des
# `ExchangeRecord` en memoire, ce sont les routeurs qui les deversent ici
# APRES l'operation metier (`services/sumup_exchange_log.persist`).
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SumUpExchange(Base):
    __tablename__ = "sumup_exchanges"

    # ping_reader, push_to_reader, checkout_status, reader_checkout_status,
    # cancel_checkout, terminate_reader, refund, get_transaction.
    operation: Mapped[str] = mapped_column(String(length=40), nullable=False)
    method: Mapped[str] = mapped_column(String(length=10), nullable=False)
    # Chemin seul, jamais la query : elle pourrait porter un secret.
    url_path: Mapped[str] = mapped_column(String(length=300), nullable=False)
    request_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Nombre d'essais avant la reponse finale (1 = aucun rejeu transport).
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # transport, timeout, http_4xx, http_5xx, decode.
    error_type: Mapped[str | None] = mapped_column(String(length=20), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkout_id: Mapped[str | None] = mapped_column(String(length=100), nullable=True)
    client_transaction_id: Mapped[str | None] = mapped_column(String(length=120), nullable=True)
    # Correlation avec la ligne de log de la requete entrante.
    request_id: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
