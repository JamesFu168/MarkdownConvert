"""Error taxonomy shared by pipeline, adapters and HTTP layer.

Permanent errors must never be retried; transient errors may be retried within the
configured budget. Both carry a stable machine-readable ``code``.
"""
from __future__ import annotations


class SdsError(Exception):
    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


class PermanentError(SdsError):
    """Will not succeed on retry (unsupported format, encrypted, schema violation, auth...)."""
    retryable = False
    http_status = 422


class TransientError(SdsError):
    """May succeed on retry (429, 5xx, network, lease conflict)."""
    retryable = True
    http_status = 503

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None,
                 retry_after_seconds: float | None = None):
        super().__init__(message, code=code, details=details)
        self.retry_after_seconds = retry_after_seconds


class UnsupportedFormatError(PermanentError):
    code = "unsupported_format"
    http_status = 415


class DocumentTooLargeError(PermanentError):
    code = "document_too_large"
    http_status = 413


class EncryptedDocumentError(PermanentError):
    code = "encrypted_document"


class CorruptDocumentError(PermanentError):
    code = "corrupt_document"


class NeedsOcrError(PermanentError):
    """Scanned / image-only document: phase 1 does not OCR, so it is a terminal outcome."""
    code = "needs_ocr"


class ProviderConfigurationError(PermanentError):
    code = "provider_not_configured"
    http_status = 500


class ProviderPermanentError(PermanentError):
    code = "provider_permanent_error"


class ProviderUnsupportedInputError(PermanentError):
    code = "unsupported_file_input"


class ProviderTransientError(TransientError):
    code = "provider_transient_error"


class ProviderTimeoutError(TransientError):
    code = "provider_timeout"


class SchemaViolationError(PermanentError):
    code = "schema_violation"


class NotFoundError(SdsError):
    code = "not_found"
    http_status = 404


class ForbiddenError(SdsError):
    code = "forbidden"
    http_status = 403


class UnauthorizedError(SdsError):
    code = "unauthorized"
    http_status = 401


class ConflictError(SdsError):
    code = "conflict"
    http_status = 409


class BadRequestError(SdsError):
    code = "bad_request"
    http_status = 400


class LeaseConflictError(TransientError):
    code = "lease_conflict"
