import pytest

from app.core.config import Settings, _validate_fiscal_key, _validate_secret_key


def _base_kwargs(**overrides) -> dict:
    kwargs = {
        "ENVIRONMENT": "development",
        "SECRET_KEY": "x" * 32,
        "FISCAL_SIGNING_KEY": "y" * 32,
    }
    kwargs.update(overrides)
    return kwargs


def test_production_requires_secret_key():
    s = Settings(**_base_kwargs(ENVIRONMENT="production", SECRET_KEY=""))
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _validate_secret_key(s)


def test_production_rejects_default_secret_key():
    s = Settings(
        **_base_kwargs(
            ENVIRONMENT="production",
            SECRET_KEY="change-me-in-production-use-openssl-rand-hex-32",
        )
    )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _validate_secret_key(s)


def test_development_generates_ephemeral_secret_key():
    s = Settings(**_base_kwargs(ENVIRONMENT="development", SECRET_KEY=""))
    _validate_secret_key(s)
    assert s.SECRET_KEY  # une cle a ete generee, pas d'exception


def test_production_requires_fiscal_signing_key():
    s = Settings(**_base_kwargs(ENVIRONMENT="production", FISCAL_SIGNING_KEY=""))
    with pytest.raises(RuntimeError, match="FISCAL_SIGNING_KEY"):
        _validate_fiscal_key(s)


def test_production_fiscal_signing_key_shorter_than_32_raises():
    s = Settings(**_base_kwargs(ENVIRONMENT="production", FISCAL_SIGNING_KEY="trop-court"))
    with pytest.raises(RuntimeError, match="FISCAL_SIGNING_KEY"):
        _validate_fiscal_key(s)


def test_non_production_fiscal_signing_key_falls_back_to_secret_key():
    s = Settings(**_base_kwargs(ENVIRONMENT="development", FISCAL_SIGNING_KEY=""))
    _validate_fiscal_key(s)
    assert s.FISCAL_SIGNING_KEY == s.SECRET_KEY
