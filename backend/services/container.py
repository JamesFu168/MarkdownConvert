"""Dependency wiring. Built once per worker process and reused across invocations."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

from adapters.markitdown_adapter import DocumentConverter, MarkItDownConverter
from adapters.mock_provider import MockProvider
from adapters.ocr_interface import NotConfiguredOcrProvider, OcrProvider
from adapters.provider_base import ExtractionProvider
from domain.models import ExperimentArm

from .config import Settings
from .cost import PricingTable, load_pricing
from .experiments import load_arms
from .jobs import JobService
from .pipeline import Processor
from .review import ReviewService
from .storage import BlobStore, QueueStore, Repository

log = logging.getLogger("sds.container")


@dataclass
class Container:
    settings: Settings
    repo: Repository
    blob: BlobStore
    queue: QueueStore
    provider: ExtractionProvider
    converter: DocumentConverter
    pricing: PricingTable
    arms: dict[str, ExperimentArm]
    ocr: OcrProvider
    jobs: JobService
    processor: Processor
    reviews: ReviewService


def build_provider(settings: Settings) -> ExtractionProvider:
    if settings.extraction_provider == "mock":
        return MockProvider(settings.mock_provider_behavior)
    settings.validate_for_provider()
    from adapters.openai_adapter import OpenAIProvider

    return OpenAIProvider(settings.openai_api_key)


def build_container(settings: Settings | None = None, *, repo: Repository | None = None, blob: BlobStore | None = None,
                    queue: QueueStore | None = None, provider: ExtractionProvider | None = None,
                    converter: DocumentConverter | None = None, pricing: PricingTable | None = None,
                    arms: dict[str, ExperimentArm] | None = None, ocr: OcrProvider | None = None) -> Container:
    settings = settings or Settings.from_env()
    if repo is None or blob is None or queue is None:
        from adapters.storage_azure import AzureBlobStore, AzureQueue, AzureTableRepository

        repo = repo or AzureTableRepository(settings.storage_connection, table_prefix=settings.table_prefix)
        blob = blob or AzureBlobStore(settings.storage_connection, settings.blob_container)
        queue = queue or AzureQueue(settings.storage_connection, settings.queue_name)
    provider = provider or build_provider(settings)
    converter = converter or MarkItDownConverter()
    pricing = pricing or load_pricing(settings.pricing_table_path)
    arms = arms or load_arms(settings.experiments_path)
    ocr = ocr or NotConfiguredOcrProvider(settings.docintel_endpoint)
    instance_id = os.environ.get("WEBSITE_INSTANCE_ID") or f"local-{os.getpid()}"
    jobs = JobService(repo, blob, queue, settings, arms, provider_name=provider.name, provider_is_test_double=provider.is_test_double,
                      converter_name=converter.name, converter_version=converter.version, pricing_version=pricing.version)
    processor = Processor(repo, blob, settings, provider, converter, pricing, ocr, instance_id=instance_id)
    reviews = ReviewService(repo, blob)
    log.info("container ready: provider=%s test_double=%s converter=%s/%s auth=%s", provider.name, provider.is_test_double,
             converter.name, converter.version, settings.auth_mode)
    return Container(settings, repo, blob, queue, provider, converter, pricing, arms, ocr, jobs, processor, reviews)


_container: Container | None = None
_lock = threading.Lock()


def get_container() -> Container:
    global _container
    if _container is None:
        with _lock:
            if _container is None:
                _container = build_container()
    return _container


def reset_container() -> None:
    global _container
    _container = None
