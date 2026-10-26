"""Seleciona a suíte do ambiente uv ativo quando pytest roda na raiz."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE_PROJECT = Path(sys.prefix).parent.name


def pytest_ignore_collect(collection_path, config):
    for project in ('agente', 'servidor-mcp'):
        if project != ACTIVE_PROJECT and collection_path.is_relative_to(ROOT / project):
            return True
    return None
