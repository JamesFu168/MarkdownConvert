import pytest

from domain.errors import ForbiddenError, ProviderConfigurationError, UnauthorizedError
from services.auth import resolve_principal
from services.config import Settings


def test_local_dev_only_on_loopback(monkeypatch):
    s = Settings(auth_mode="local_dev", local_dev_user_id="dev")
    assert resolve_principal({"host": "localhost:7071"}, s).owner_id == "dev"
    assert resolve_principal({"Host": "127.0.0.1:7071"}, s).owner_id == "dev"
    with pytest.raises(ForbiddenError):
        resolve_principal({"host": "myapp.azurewebsites.net"}, s)
    with pytest.raises(ForbiddenError):
        resolve_principal({"host": "localhost", "x-forwarded-for": "1.2.3.4"}, s)
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "abc")
    with pytest.raises(ProviderConfigurationError):
        resolve_principal({"host": "localhost"}, s)


def test_easyauth_requires_platform_header():
    s = Settings(auth_mode="easyauth")
    with pytest.raises(UnauthorizedError):
        resolve_principal({"host": "app.azurewebsites.net"}, s)
    p = resolve_principal({"x-ms-client-principal-id": "oid-1", "x-ms-client-principal-name": "u@example.com"}, s)
    assert p.owner_id == "oid-1" and p.display_name == "u@example.com"
