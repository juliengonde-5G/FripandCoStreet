# Nouveau schema (PR2, §4.1/§4.3/§5 du contrat) — entrees Pydantic pour les
# routes /api/pos/* hors bloc CB (`cb_router.py`, agent B).
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CartItemIn(BaseModel):
    label: str | None = None
    unit_price: Decimal = Field(ge=0, decimal_places=2)
    quantity: int = Field(default=1, ge=1, le=99)


class DiscountIn(BaseModel):
    type: Literal["percent", "amount"]
    value: Decimal = Field(gt=0)


class PaymentIn(BaseModel):
    method: Literal["cash", "card"]
    amount: Decimal = Field(gt=0, decimal_places=2)
    tendered_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    checkout_id: str | None = None


class CreateTransactionRequest(BaseModel):
    client_uuid: uuid.UUID
    items: list[CartItemIn] = Field(min_length=1, max_length=50)
    discount: DiscountIn | None = None
    payments: list[PaymentIn] = Field(min_length=1)
    # PR7/I3 — fiche cliente choisie en caisse AVANT l'encaissement. Le
    # rattachement est pose des l'INSERT et reste HORS SIGNATURE (comme le
    # rattachement a posteriori de PR3) : `client_id` n'entre pas dans
    # `fiscal.py::_transaction_payload`. Ne pas confondre avec
    # `client_uuid`, qui est la cle d'idempotence generee par le navigateur
    # et qui, elle, est signee.
    client_id: uuid.UUID | None = None


class DenominationIn(BaseModel):
    denom: Decimal
    count: int = Field(ge=0)


class OpenDrawerRequest(BaseModel):
    opening_amount: Decimal = Field(ge=0, decimal_places=2)
    breakdown: list[DenominationIn] | None = None


class CloseDrawerRequest(BaseModel):
    closing_amount: Decimal = Field(ge=0, decimal_places=2)
    breakdown: list[DenominationIn] | None = None
    note: str | None = None


class CashMovementIn(BaseModel):
    direction: Literal["in", "out"]
    amount: Decimal = Field(gt=0, decimal_places=2)
    reason: Literal["bank_deposit", "supplier_payment", "float_top_up", "other"]
    note: str | None = None


class CancelTransactionRequest(BaseModel):
    reason: str = Field(min_length=3)


class RegularizationRequest(BaseModel):
    reason: str = Field(min_length=3)
    period_from: str
    period_to: str

    @field_validator("period_from", "period_to")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("period_from/period_to requis (ISO 8601)")
        return value
