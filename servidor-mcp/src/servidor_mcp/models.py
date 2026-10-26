"""Modelos de saída publicados no catálogo MCP."""

from pydantic import BaseModel


class Room(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class RoomList(BaseModel):
    salas: list[Room]


class Conflict(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Availability(BaseModel):
    sala: str
    livre: bool
    conflitos: list[Conflict]


class Booking(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None
