# Nouveau modele — Journal des Evenements Techniques (JET), pilier
# "securisation" de l'auto-attestation NF525 (voir docs/COMPLIANCE_NF525.md
# cote Vintiz et le cahier des charges Frip & Co Street §3.1). Modelise sur
# le chainage HMAC de app/services/fiscal.py (signature v2) de Vintiz, mais
# generique : un seul journal couvre tous les evenements techniques
# (connexion, deconnexion, rate-limit, demarrage...) au lieu d'un journal
# par type d'objet metier.
import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class JournalEvent(Base):
    """Une ligne immuable du journal des evenements techniques.

    L'immuabilite (interdiction d'UPDATE/DELETE) est appliquee cote base par
    le trigger `trg_protect_journal_event` (migration 0001) — pas seulement
    par convention applicative.
    """

    __tablename__ = "journal_events"

    seq: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Snapshot du nom d'utilisateur au moment de l'evenement — l'entree du
    # journal reste lisible meme si le compte est renomme ou supprime plus tard.
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
