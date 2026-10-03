"""Settings loaded from environment variables (App Settings / local.settings.json / .env).

Model names are never guessed: when ``EXTRACTION_PROVIDER=openai`` and a model name is
missing the service fails loudly instead of silently substituting a default or a mock.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from domain.errors import ProviderConfigurationError
from domain.models import ModelRole

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v is not None else default


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v is not None else default


@dataclass(frozen=True)
class Settings:
    extraction_provider: str = "mock"
    openai_api_key: str | None = None
    openai_model_baseline: str | None = None
    openai_model_candidate: str | None = None
    openai_timeout_seconds: float = 180.0
    openai_max_attempts: int = 2
    openai_file_detail: str | None = None
    openai_max_output_tokens: int = 8192
    max_upload_bytes: int = 20 * 1024 * 1024
    processing_lease_seconds: int = 600
    min_text_chars_per_page: int = 40
    storage_connection: str = "UseDevelopmentStorage=true"
    blob_container: str = "sds"
    queue_name: str = "sds-jobs"
    table_prefix: str = "sds"
    auth_mode: str = "local_dev"
    local_dev_user_id: str = "local-dev-user"
    cors_allowed_origins: tuple[str, ...] = ("http://localhost:4200",)
    test_sds_dir: str | None = None
    docintel_endpoint: str | None = None
    mock_provider_behavior: str = "success"
    pricing_table_path: str = str(REPO_DIR / "evaluation" / "pricing.json")
    experiments_path: str = str(REPO_DIR / "evaluation" / "experiments" / "pilot-v1.json")
    prompt_version: str = "extraction_v1"
    max_retries_per_job: int = 3
    gold_dir: str = str(REPO_DIR / "data" / "gold")
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        provider = (_env("EXTRACTION_PROVIDER", "mock") or "mock").lower()
        if provider not in ("mock", "openai"):
            raise ProviderConfigurationError(f"EXTRACTION_PROVIDER must be 'mock' or 'openai', got {provider!r}")
        detail = _env("OPENAI_FILE_DETAIL")
        if detail and detail not in ("auto", "low", "high"):
            raise ProviderConfigurationError("OPENAI_FILE_DETAIL must be auto|low|high or empty")
        origins = tuple(o.strip() for o in (_env("CORS_ALLOWED_ORIGINS", "http://localhost:4200") or "").split(",") if o.strip())
        return cls(
            extraction_provider=provider,
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_model_baseline=_env("OPENAI_MODEL_BASELINE"),
            openai_model_candidate=_env("OPENAI_MODEL_CANDIDATE"),
            openai_timeout_seconds=_float("OPENAI_TIMEOUT_SECONDS", 180.0),
            openai_max_attempts=_int("OPENAI_MAX_ATTEMPTS", 2),
            openai_file_detail=detail,
            openai_max_output_tokens=_int("OPENAI_MAX_OUTPUT_TOKENS", 8192),
            max_upload_bytes=_int("MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
            processing_lease_seconds=_int("PROCESSING_LEASE_SECONDS", 600),
            min_text_chars_per_page=_int("MIN_TEXT_CHARS_PER_PAGE", 40),
            storage_connection=_env("AzureWebJobsStorage", "UseDevelopmentStorage=true") or "UseDevelopmentStorage=true",
            blob_container=_env("SDS_BLOB_CONTAINER", "sds") or "sds",
            queue_name=_env("SDS_QUEUE_NAME", "sds-jobs") or "sds-jobs",
            table_prefix=_env("SDS_TABLE_PREFIX", "sds") or "sds",
            auth_mode=(_env("AUTH_MODE", "local_dev") or "local_dev").lower(),
            local_dev_user_id=_env("LOCAL_DEV_USER_ID", "local-dev-user") or "local-dev-user",
            cors_allowed_origins=origins,
            test_sds_dir=_env("TEST_SDS_DIR"),
            docintel_endpoint=_env("DOCINTEL_ENDPOINT"),
            mock_provider_behavior=(_env("MOCK_PROVIDER_BEHAVIOR", "success") or "success").lower(),
            pricing_table_path=_env("PRICING_TABLE_PATH", str(REPO_DIR / "evaluation" / "pricing.json")) or "",
            experiments_path=_env("EXPERIMENTS_PATH", str(REPO_DIR / "evaluation" / "experiments" / "pilot-v1.json")) or "",
            prompt_version=_env("PROMPT_VERSION", "extraction_v1") or "extraction_v1",
            max_retries_per_job=_int("MAX_RETRIES_PER_JOB", 3),
            gold_dir=_env("GOLD_DIR", str(REPO_DIR / "data" / "gold")) or "",
        )

    def validate_for_provider(self) -> None:
        """Fail fast when the real provider is selected without the required settings."""
        if self.extraction_provider == "openai":
            missing = [n for n, v in (
                ("OPENAI_API_KEY", self.openai_api_key),
                ("OPENAI_MODEL_BASELINE", self.openai_model_baseline),
                ("OPENAI_MODEL_CANDIDATE", self.openai_model_candidate),
            ) if not v]
            if missing:
                raise ProviderConfigurationError(
                    "EXTRACTION_PROVIDER=openai but required settings are missing: " + ", ".join(missing),
                    details={"missing": missing},
                )

    def model_for(self, role: ModelRole) -> str:
        """Model name for an experiment arm. For the mock provider a labelled placeholder is used."""
        name = self.openai_model_baseline if role == ModelRole.BASELINE else self.openai_model_candidate
        if self.extraction_provider == "mock":
            return name or f"mock-{role.value}"
        if not name:
            var = "OPENAI_MODEL_BASELINE" if role == ModelRole.BASELINE else "OPENAI_MODEL_CANDIDATE"
            raise ProviderConfigurationError(f"{var} is not configured", details={"missing": [var]})
        return name

    @property
    def running_on_azure(self) -> bool:
        return bool(os.environ.get("WEBSITE_INSTANCE_ID") or os.environ.get("WEBSITE_SITE_NAME"))
