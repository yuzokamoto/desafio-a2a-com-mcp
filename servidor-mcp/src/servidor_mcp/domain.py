"""Regras de reserva e dados locais da central de salas."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

INVALID_TIME = 'Horario invalido: use ISO 8601 com fuso, ex. 2026-11-03T14:00:00-03:00'
INVALID_INTERVAL = 'Intervalo invalido: fim deve ser posterior a inicio'
OUTSIDE_WINDOW = 'Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00'
TOO_LONG = 'Duracao acima do limite: a politica permite no maximo 2 horas'
NO_ALTERNATIVES = 'Sem alternativas disponiveis no intervalo'
LOCAL_ZONE = timezone(timedelta(hours=-3))


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ValueError(INVALID_TIME) from None


class BookingDomain:
    def __init__(self, data_dir: Path):
        self.rooms = json.loads((data_dir / 'salas.json').read_text())
        self.bookings = json.loads((data_dir / 'reservas.json').read_text())
        self.policy = (data_dir / 'politica-de-uso.md').read_text()
        self.policy_version = self.policy.splitlines()[0].split(':', 1)[1].strip()
        self.next_id = max((int(item['id'].split('-')[1]) for item in self.bookings), default=0) + 1

    def validate(self, room: str, start: str, end: str):
        if room not in {item['id'] for item in self.rooms}:
            raise ValueError(f'Sala inexistente: {room}')
        first, last = parse_time(start), parse_time(end)
        if last <= first:
            raise ValueError(INVALID_INTERVAL)
        local_first, local_last = first.astimezone(LOCAL_ZONE), last.astimezone(LOCAL_ZONE)
        if (local_first.date() != local_last.date() or local_first.hour < 8
                or local_last.time() > datetime.min.replace(hour=20).time()):
            raise ValueError(OUTSIDE_WINDOW)
        if last - first > timedelta(hours=2):
            raise ValueError(TOO_LONG)
        return first, last

    def conflicts(self, room: str, start: str, end: str) -> list[dict]:
        first, last = self.validate(room, start, end)
        return [{key: item[key] for key in ('id', 'inicio', 'fim', 'responsavel')}
                for item in self.bookings if item['sala'] == room
                and parse_time(item['inicio']) < last and first < parse_time(item['fim'])]

    def alternatives(self, room: str, start: str, end: str) -> list[str]:
        capacity = next(item['capacidade'] for item in self.rooms if item['id'] == room)
        return [item['id'] for item in sorted(self.rooms, key=lambda item: (item['capacidade'], item['id']))
                if item['id'] != room and item['capacidade'] >= capacity
                and not self.conflicts(item['id'], start, end)][:3]

    def book(self, room: str, start: str, end: str, owner: str) -> dict:
        self.validate(room, start, end)
        if self.conflicts(room, start, end):
            raise ValueError('Sala ocupada no intervalo')
        booking = dict(id=f'res-{self.next_id:04d}', sala=room, inicio=start, fim=end, responsavel=owner)
        self.next_id += 1
        self.bookings.append(booking)
        return dict(reserva=booking['id'], reservado=True, sala=room, inicio=start, fim=end,
                    responsavel=owner, politica=self.policy_version, motivo=None)
