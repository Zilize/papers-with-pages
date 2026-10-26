"""GitHub credentials live in the OS vault, separately from collection files."""

import hashlib
import os
import sys


class TokenStoreError(RuntimeError):
    """The OS credential service could not complete an operation."""


def _backend():
    # Select only system-backed stores, never third-party plaintext fallbacks.
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    from keyring.backends.SecretService import Keyring

    return Keyring()


class TokenStore:
    service = "papers-with-pages"

    def __init__(self, root):
        scope = os.path.normcase(str(root.resolve())).encode("utf-8")
        self.account = "github:" + hashlib.sha256(scope).hexdigest()

    def load(self):
        try:
            return _backend().get_password(self.service, self.account)
        except Exception:
            raise TokenStoreError("Cannot read the system credential store") from None

    def save(self, token):
        try:
            backend = _backend()
            if token:
                backend.set_password(self.service, self.account, token)
            elif backend.get_password(self.service, self.account) is not None:
                backend.delete_password(self.service, self.account)
        except Exception:
            raise TokenStoreError(
                "Cannot update the system credential store; token was not saved"
            ) from None
