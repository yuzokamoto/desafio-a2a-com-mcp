"""Configuração do processo e da proteção de estado."""

import os
from pathlib import Path

STATE_TTL = 600
DATA_DIR = Path(__file__).resolve().parents[3] / 'dados'
SECRET_HINT = "REQUEST_STATE_SECRET exige ao menos 32 bytes. Gere com: python3 -c 'import secrets; print(secrets.token_hex(32))'"


def load_secret() -> bytes:
    value = os.environ.get('REQUEST_STATE_SECRET', '')
    try:
        secret = bytes.fromhex(value)
    except ValueError:
        secret = value.encode()
    if len(secret) < 32:
        raise ValueError(SECRET_HINT)
    return secret
