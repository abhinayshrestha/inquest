"""Security configuration a hosted (APP_ENV=production) process refuses to start without
(D041). Local development keeps its defaults: each of these is enforced elsewhere as a
refusal at use time, but a hosted server should not come up half-configured and fail one
request at a time. The database role itself is checked separately (check_runtime_role,
D040). Messages name settings, never values.
"""

from src.auth.settings import AuthNotConfigured, load_auth_settings
from src.config import Settings
from src.credentials.settings import CredentialStorageUnavailable, load_credential_config
from src.mcp_server.auth import McpSettings, secret_problem


def deployment_problems(settings: Settings, mcp: McpSettings) -> list[str]:
    problems = []
    if not settings.inquest_db_user or not settings.inquest_db_password:
        problems.append("INQUEST_DB_USER and INQUEST_DB_PASSWORD (the runtime role) are not set")
    # The process only needs the runtime role; anything more is reachable by any code
    # execution in it (audit H4/H5). Migrations and provisioning run as separate jobs.
    if settings.postgres_password is not None:
        problems.append("POSTGRES_PASSWORD (administrative role) is set for the application")
    if settings.inquest_db_migration_password is not None:
        problems.append("INQUEST_DB_MIGRATION_PASSWORD is set for the application")
    if (problem := secret_problem(mcp)) is not None:
        problems.append(problem)
    try:
        load_auth_settings()
    except AuthNotConfigured as exc:
        problems.append(f"sign-in: {exc}")
    try:
        load_credential_config()
    except CredentialStorageUnavailable as exc:
        problems.append(f"credential storage: {exc}")
    return problems


def check_deployment(settings: Settings, mcp: McpSettings) -> None:
    if settings.app_env != "production":
        return
    if problems := deployment_problems(settings, mcp):
        raise RuntimeError(f"refusing to start: {'; '.join(problems)}")
