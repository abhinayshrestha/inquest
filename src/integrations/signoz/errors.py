from enum import StrEnum


class SigNozErrorCode(StrEnum):
    CONFIG = "signoz_config"
    AUTH = "signoz_auth"
    INVALID_QUERY = "signoz_invalid_query"
    TIMEOUT = "signoz_timeout"
    UNAVAILABLE = "signoz_unavailable"
    RESPONSE_TOO_LARGE = "signoz_response_too_large"
    MALFORMED_RESPONSE = "signoz_malformed_response"


RETRYABLE_CODES = {SigNozErrorCode.TIMEOUT, SigNozErrorCode.UNAVAILABLE}


class SigNozError(Exception):
    """Message is already sanitized and safe to store in ToolCall.error_message."""

    def __init__(self, code: SigNozErrorCode, message: str, *, http_status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status

    @property
    def retryable(self) -> bool:
        # The adapter never retries; the tool executor decides using this flag.
        return self.code in RETRYABLE_CODES
