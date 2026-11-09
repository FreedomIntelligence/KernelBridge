"""Storage-side failures that adapters may report to application services."""
from __future__ import annotations


class RepositoryError(RuntimeError):
    code = "REPOSITORY_ERROR"


class CorruptRecordError(RepositoryError):
    code = "CORRUPT_RECORD"


class ArtifactUnavailableError(RepositoryError):
    code = "ARTIFACT_UNAVAILABLE"


class IntegrityError(RepositoryError):
    code = "INTEGRITY_ERROR"


class ApplicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 2,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
