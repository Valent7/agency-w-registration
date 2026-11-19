"""Authenticated backup using the existing Agency Fernet key, never a new secret."""
import json
from cryptography.fernet import Fernet, InvalidToken
from agent_memory import MemoryError, canonical, digest


def encrypt_backup(bundle, key):
    if not key:
        raise MemoryError('Existing encryption key is required')
    return Fernet(key.encode() if isinstance(key,str) else key).encrypt(canonical(bundle).encode())


def decrypt_backup(data, key):
    try:
        raw = Fernet(key.encode() if isinstance(key,str) else key).decrypt(data)
        bundle = json.loads(raw)
        if digest({k:v for k,v in bundle.items() if k!='checksum'}) != bundle.get('checksum'):
            raise ValueError()
        return bundle
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise MemoryError('Backup is damaged or encrypted for a different installation') from None
