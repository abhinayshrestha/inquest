from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.integrations.signoz.errors import SigNozError, SigNozErrorCode


class SigNozSettings(BaseSettings):
    """Separate from core Settings so the app and DB tests run without SigNoz configured."""

    model_config = SettingsConfigDict(
        env_prefix="SIGNOZ_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    base_url: str
    api_key: SecretStr
    timeout_seconds: float = 15.0
    max_response_bytes: int = 2_000_000


def load_signoz_settings() -> SigNozSettings:
    try:
        settings = SigNozSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Report field names only; pydantic's message would echo the provided values.
        fields = sorted({f"SIGNOZ_{'_'.join(map(str, e['loc'])).upper()}" for e in exc.errors()})
        raise SigNozError(
            SigNozErrorCode.CONFIG, f"invalid or missing settings: {', '.join(fields)}"
        ) from None
    if not settings.base_url.startswith(("http://", "https://")):
        raise SigNozError(SigNozErrorCode.CONFIG, "SIGNOZ_BASE_URL must start with http(s)://")
    return settings
