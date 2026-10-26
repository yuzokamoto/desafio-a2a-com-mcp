"""Gramática determinística dos pedidos e continuações."""

import re

INVALID_REQUEST = 'Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>'
BOOKING = re.compile(r'^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+)$')


def parse_booking(text: str) -> dict | None:
    match = BOOKING.fullmatch(text.strip())
    return dict(zip(('sala', 'inicio', 'fim', 'responsavel'), match.groups())) if match else None


def parse_choice(text: str) -> str | None:
    match = re.fullmatch(r'escolha=(\S+)', text.strip())
    return match.group(1) if match else None
