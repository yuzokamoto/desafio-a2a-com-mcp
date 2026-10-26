"""Verifica as regras de negócio sem transporte."""

import secrets
import pytest
from servidor_mcp.config import DATA_DIR, STATE_TTL, load_secret
from servidor_mcp.domain import (BookingDomain, INVALID_TIME, INVALID_INTERVAL, OUTSIDE_WINDOW, TOO_LONG)


def hour(value):
    return f'2026-11-03T{value}:00-03:00'


@pytest.fixture
def domain():
    return BookingDomain(DATA_DIR)


@pytest.mark.parametrize('room,start,end,message', [
    ('x', hour('09:00'), hour('10:00'), 'Sala inexistente: x'),
    ('sala-aquario', 'invalido', hour('10:00'), INVALID_TIME),
    ('sala-aquario', '2026-11-03T09:00:00', hour('10:00'), INVALID_TIME),
    ('sala-aquario', hour('10:00'), hour('09:00'), INVALID_INTERVAL),
    ('sala-aquario', hour('07:00'), hour('08:00'), OUTSIDE_WINDOW),
    ('sala-aquario', hour('19:00'), hour('20:01'), OUTSIDE_WINDOW),
    ('sala-aquario', hour('09:00'), hour('12:00'), TOO_LONG),
    ('sala-aquario', hour('19:00'), '2026-11-04T09:00:00-03:00', OUTSIDE_WINDOW),
])
def test_validation(domain, room, start, end, message):
    with pytest.raises(ValueError) as error:
        domain.validate(room, start, end)
    assert str(error.value) == message


def test_edges_ids_conflicts(domain):
    assert domain.book('sala-aquario', hour('08:00'), hour('09:00'), 'Doc')['reserva'] == 'res-0003'
    domain.book('sala-aquario', hour('18:00'), hour('20:00'), 'Doc')
    assert not domain.conflicts('sala-garagem', hour('15:00'), hour('16:00'))
    assert domain.conflicts('sala-garagem', hour('14:00'), hour('15:00'))[0]['id'] == 'res-0001'
    assert domain.policy_version == '2026-11-01'
    # O deslocamento é convertido antes de verificar a janela local.
    domain.validate('sala-aquario', '2026-11-03T11:00:00Z', '2026-11-03T12:00:00Z')


def test_alternatives(domain):
    assert domain.alternatives('sala-garagem', hour('14:00'), hour('15:00')) == ['sala-fusca', 'sala-mirante']
    domain.book('sala-fusca', hour('14:00'), hour('15:00'), 'Doc')
    assert domain.alternatives('sala-garagem', hour('14:00'), hour('15:00')) == ['sala-mirante']
    domain.book('sala-mirante', hour('14:00'), hour('15:00'), 'Doc')
    assert domain.alternatives('sala-garagem', hour('14:00'), hour('15:00')) == []
    assert len(domain.alternatives('sala-aquario', hour('10:00'), hour('11:00'))) == 3


def test_secret(monkeypatch):
    monkeypatch.delenv('REQUEST_STATE_SECRET', raising=False)
    with pytest.raises(ValueError, match='secrets.token_hex'):
        load_secret()
    monkeypatch.setenv('REQUEST_STATE_SECRET', 'curta')
    with pytest.raises(ValueError, match='secrets.token_hex'):
        load_secret()
    secret = secrets.token_hex(32)
    monkeypatch.setenv('REQUEST_STATE_SECRET', secret)
    assert len(load_secret()) == 32
    monkeypatch.setenv('REQUEST_STATE_SECRET', 'z' * 32)
    assert len(load_secret()) == 32
    assert STATE_TTL == 600
