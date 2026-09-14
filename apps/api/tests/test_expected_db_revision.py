# Extrait de Vintiz (apps/api/tests/test_expected_db_revision.py)
"""Garde-fou : EXPECTED_DB_REVISION doit suivre le head Alembic.

En production, `app.main` refuse de demarrer si la revision de la base ne
correspond pas a `EXPECTED_DB_REVISION` (schema migration-owned, JET/NF525).
Toute nouvelle migration DOIT donc bumper cette constante. Ce test echoue si
on ajoute une migration sans mettre a jour `app/version.py` — ce qui, sinon,
casse le boot de l'API en prod apres deploiement.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.version import EXPECTED_DB_REVISION


def test_expected_db_revision_matches_alembic_head():
    alembic_dir = Path(__file__).resolve().parents[1] / "alembic"
    cfg = Config()
    cfg.set_main_option("script_location", str(alembic_dir))
    script = ScriptDirectory.from_config(cfg)

    heads = script.get_heads()
    assert len(heads) == 1, f"Attendu un seul head Alembic, trouve : {heads}"
    assert EXPECTED_DB_REVISION == heads[0], (
        f"EXPECTED_DB_REVISION={EXPECTED_DB_REVISION!r} ne correspond pas au "
        f"head Alembic {heads[0]!r}. Bumper app/version.py apres toute migration."
    )
