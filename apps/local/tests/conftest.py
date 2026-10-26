"""Keep application tests away from the user's real credential store."""

import pytest


@pytest.fixture(autouse=True)
def credential_backend(monkeypatch):
    class MemoryVault:
        def __init__(self):
            self.values = {}

        def get_password(self, service, account):
            return self.values.get((service, account))

        def set_password(self, service, account, password):
            self.values[service, account] = password

        def delete_password(self, service, account):
            del self.values[service, account]

    backend = MemoryVault()
    monkeypatch.setattr("app.credentials._backend", lambda: backend)
    return backend
