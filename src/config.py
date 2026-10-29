from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """All values come from the environment / .env. No defaults on purpose, except the
    optional least-privilege database roles below."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "production"]
    log_level: str

    # The bootstrap (administrative) PostgreSQL role: provisions roles and ownership. The
    # application uses it only while the roles below are not configured (D040). A hosted
    # application process must not receive its password (D041), so both are optional.
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_test_db: str | None = None
    database_echo: bool

    # Least-privilege roles (D040, scripts.provision_database): the running application
    # (data access only) and Alembic (owns the schema). Each needs its password too.
    inquest_db_user: str | None = None
    inquest_db_password: SecretStr | None = None
    inquest_db_migration_user: str | None = None
    inquest_db_migration_password: SecretStr | None = None

    @field_validator(
        "inquest_db_user",
        "inquest_db_password",
        "inquest_db_migration_user",
        "inquest_db_migration_password",
        "postgres_user",
        "postgres_password",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # .env.example leaves them empty; a user without a password must not pass.
        return value or None

    @property
    def database_name(self) -> str:
        if self.app_env != "test":
            return self.postgres_db
        if not self.postgres_test_db:
            raise ValueError("POSTGRES_TEST_DB must be set when APP_ENV=test")
        return self.postgres_test_db

    def _url(self, user: str, password: SecretStr) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.database_name,
        )

    def _role_url(self, name: str, user: str | None, password: SecretStr | None) -> URL:
        if (user is None) != (password is None):
            # Not a pydantic validator: its error would echo every setting, passwords too.
            raise ValueError(f"{name} and {name.replace('USER', 'PASSWORD')} must be set together")
        return self._url(user, password) if user and password else self.admin_database_url

    @property
    def database_url(self) -> URL:
        """The running application's connection: the runtime role when configured."""
        return self._role_url("INQUEST_DB_USER", self.inquest_db_user, self.inquest_db_password)

    @property
    def migration_database_url(self) -> URL:
        """Alembic's connection: the schema-owning migration role when configured."""
        return self._role_url(
            "INQUEST_DB_MIGRATION_USER",
            self.inquest_db_migration_user,
            self.inquest_db_migration_password,
        )

    @property
    def admin_database_url(self) -> URL:
        if not self.postgres_user or not self.postgres_password:
            raise ValueError("POSTGRES_USER and POSTGRES_PASSWORD must be set for this connection")
        return self._url(self.postgres_user, self.postgres_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
