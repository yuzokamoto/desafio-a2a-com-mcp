"""Identificadores W3C propagados por tarefa."""

import re
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Trace:
    trace_id: str
    flags: str = '01'

    def parent(self) -> str:
        return f'00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}'


def parse_traceparent(value: str | None) -> Trace | None:
    match = re.fullmatch(r'00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})', value or '')
    if not match or int(match[1], 16) == 0 or int(match[2], 16) == 0:
        return None
    return Trace(match[1], match[3])


def new_trace() -> Trace:
    return Trace(secrets.token_hex(16))
