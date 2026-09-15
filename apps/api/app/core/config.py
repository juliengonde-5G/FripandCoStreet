# Extrait de Vintiz (apps/api/app/core/config.py) — perimetre reduit a la
# caisse Frip & Co Street (mono-compte, sans IA/SMTP/Twilio/Redis/uploads).
import logging
import os
import secrets

from pydantic_settings import BaseSettings

logger = logging.getLogger("fripco")

# Valeur sentinelle indiquant un SECRET_KEY non defini (refuse en production).
_INSECURE_DEFAULT_KEY = "change-me-in-production-use-openssl-rand-hex-32"


class Settings(BaseSettings):
    """Configuration applicative chargee depuis les variables d'environnement."""

    # Environnement : 'development' | 'test' | 'production'
    ENVIRONMENT: str = "development"

    # Base de donnees — role applicatif non-proprietaire (`fripco_app`,
    # sans droit DDL). Les migrations utilisent MIGRATION_DATABASE_URL
    # (role proprietaire) quand elle est definie, sinon DATABASE_URL.
    DATABASE_URL: str = "postgresql+asyncpg://fripco_app:fripco_app@localhost:5432/fripco"
    MIGRATION_DATABASE_URL: str = ""

    # Auth / JWT
    SECRET_KEY: str = _INSECURE_DEFAULT_KEY
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # Cle HMAC dediee et stable pour le journal des evenements techniques
    # (JET, chainage NF525). Ne doit jamais etre tournee sans avoir au
    # prealable clos/archive la chaine active.
    FISCAL_SIGNING_KEY: str = ""

    # CORS — liste vide par defaut (deploiement same-origin derriere Caddy)
    CORS_ORIGINS: str = ""

    # Rate limit /auth/login
    LOGIN_RATE_LIMIT_ATTEMPTS: int = 10
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 300

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # Identite boutique
    SHOP_NAME: str = "Frip & Co Street"

    # SumUp CB — TPE Solo, push reader uniquement (pas de mode « lien de
    # paiement », D7). Secrets exclusivement en variables d'environnement
    # (D12) : jamais dans app_settings, jamais saisis depuis l'admin.
    # SUMUP_API_BASE est la RACINE de l'hote SumUp, SANS suffixe de version :
    # tous les appels (v0.1 readers/checkouts, v2.1 transactions, v1.0
    # refunds) sont construits a partir d'elle (app/services/sumup_service.py
    # ::_url). A ne changer que pour les tests (ex. un faux serveur local
    # exercant le flux complet push -> poll -> PAID -> refund) ; en
    # production, laisser la valeur par defaut.
    SUMUP_API_KEY: str = ""
    SUMUP_MERCHANT_CODE: str = ""
    SUMUP_READER_ID: str = ""
    SUMUP_API_BASE: str = "https://api.sumup.com"

    # E-mail (PR3) — passerelle Brevo -> SMTP -> simulation (E6). Le compte
    # Brevo est PARTAGE avec Vintiz Vernon (Julien) : Fripco n'ecrit jamais
    # sur la blocklist globale d'un contact ni ne supprime un contact Brevo,
    # seulement sur SA liste dediee (BREVO_LIST_ID, E1). BREVO_API_BASE est
    # la RACINE de l'hote Brevo (comme SUMUP_API_BASE), a ne changer que
    # pour les tests de bout en bout (faux serveur Brevo local).
    BREVO_API_KEY: str = ""
    BREVO_API_BASE: str = "https://api.brevo.com"
    BREVO_LIST_ID: str = ""
    # Token partage authentifiant POST /api/brevo/webhook (E9) — sans lui,
    # le webhook refuse tout evenement (403).
    BREVO_WEBHOOK_TOKEN: str = ""
    # Confirmation que le compte Brevo PARTAGE est bascule en suivi anonyme
    # (recommandation CNIL 2026 sur le pixel d'ouverture individuel). Tant
    # qu'elle n'est pas posee, tout envoi via Brevo est refuse — repli SMTP
    # puis simulation (E6). Question ouverte pour Julien.
    BREVO_ANONYMOUS_TRACKING: bool = False

    EMAIL_FROM_ADDRESS: str = "noreply@fripco-street.fr"
    EMAIL_FROM_NAME: str = "Frip & Co Street"

    # SMTP — repli si Brevo est absent ou refuse l'envoi (E6). Jamais de
    # secret en base (E10) : variables d'environnement uniquement.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [s.strip() for s in self.CORS_ORIGINS.split(",") if s.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def build_sha(self) -> str:
        return os.environ.get("FRIPCO_BUILD_SHA", "unknown")

    @property
    def build_date(self) -> str:
        return os.environ.get("FRIPCO_BUILD_DATE", "unknown")

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def _validate_secret_key(s: Settings) -> None:
    """Refuse de demarrer en production avec un SECRET_KEY absent/par defaut.

    Hors production, une cle ephemere est generee a la volee (avec warning)
    pour que les developpeurs ne dependent pas accidentellement d'une
    valeur codee en dur.

    Prend `s` en parametre (plutot que de fermer sur le singleton `settings`)
    pour rester testable unitairement sur une instance jetable — voir
    `tests/test_config.py`.
    """
    if s.SECRET_KEY in {"", _INSECURE_DEFAULT_KEY}:
        if s.is_production:
            raise RuntimeError(
                "SECRET_KEY is unset or uses the insecure default. "
                "Set SECRET_KEY in your environment "
                "(generate via: python -c 'import secrets; print(secrets.token_hex(32))')."
            )
        ephemeral = os.environ.get("FRIPCO_DEV_EPHEMERAL_KEY")
        if not ephemeral:
            ephemeral = secrets.token_hex(32)
            os.environ["FRIPCO_DEV_EPHEMERAL_KEY"] = ephemeral
        s.SECRET_KEY = ephemeral
        logger.warning(
            "SECRET_KEY not set — generated an ephemeral key for development. "
            "DO NOT use this in production."
        )
    elif len(s.SECRET_KEY) < 32:
        logger.warning(
            "SECRET_KEY is shorter than 32 characters; consider rotating to a longer secret."
        )


_validate_secret_key(settings)


def _validate_fiscal_key(s: Settings) -> None:
    """Meme logique que `_validate_secret_key`, pour FISCAL_SIGNING_KEY."""
    if not s.FISCAL_SIGNING_KEY:
        if s.is_production:
            raise RuntimeError(
                "FISCAL_SIGNING_KEY is required in production. Generate a stable "
                "64-character secret and archive it under restricted access."
            )
        s.FISCAL_SIGNING_KEY = s.SECRET_KEY
        logger.warning(
            "FISCAL_SIGNING_KEY not set — using the development SECRET_KEY. "
            "Do not persist fiscal test data across key changes."
        )
    elif len(s.FISCAL_SIGNING_KEY) < 32:
        raise RuntimeError("FISCAL_SIGNING_KEY must contain at least 32 characters")


_validate_fiscal_key(settings)
