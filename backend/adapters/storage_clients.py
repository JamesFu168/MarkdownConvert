"""Builds Azure Storage service clients from either a connection string (Azurite / local) or
identity-based settings (``AzureWebJobsStorage__accountName`` + Managed Identity on Azure)."""
from __future__ import annotations

import os
from typing import Any

from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import BinaryBase64EncodePolicy, QueueClient


def _credential() -> Any:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _is_connection_string(value: str | None) -> bool:
    return bool(value) and ("AccountKey=" in value or value.startswith("UseDevelopmentStorage"))


def identity_account_name() -> str | None:
    return os.environ.get("AzureWebJobsStorage__accountName") or os.environ.get("SDS_STORAGE_ACCOUNT_NAME")


def blob_service(connection: str | None) -> BlobServiceClient:
    if _is_connection_string(connection):
        return BlobServiceClient.from_connection_string(connection)  # type: ignore[arg-type]
    account = identity_account_name()
    if not account:
        raise RuntimeError("No storage connection string and no AzureWebJobsStorage__accountName for identity-based access")
    return BlobServiceClient(account_url=f"https://{account}.blob.core.windows.net", credential=_credential())


def table_service(connection: str | None) -> TableServiceClient:
    if _is_connection_string(connection):
        return TableServiceClient.from_connection_string(connection)  # type: ignore[arg-type]
    account = identity_account_name()
    if not account:
        raise RuntimeError("No storage connection string and no AzureWebJobsStorage__accountName for identity-based access")
    return TableServiceClient(endpoint=f"https://{account}.table.core.windows.net", credential=_credential())


def queue_client(connection: str | None, queue_name: str) -> QueueClient:
    if _is_connection_string(connection):
        return QueueClient.from_connection_string(connection, queue_name, message_encode_policy=BinaryBase64EncodePolicy())  # type: ignore[arg-type]
    account = identity_account_name()
    if not account:
        raise RuntimeError("No storage connection string and no AzureWebJobsStorage__accountName for identity-based access")
    return QueueClient(account_url=f"https://{account}.queue.core.windows.net", queue_name=queue_name, credential=_credential(),
                       message_encode_policy=BinaryBase64EncodePolicy())
