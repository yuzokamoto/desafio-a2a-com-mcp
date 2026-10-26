"""Casos da gramática e do contexto de rastreamento."""

import pytest
from agente.grammar import parse_booking, parse_choice
from agente.tracing import Trace, parse_traceparent


@pytest.mark.parametrize('value', ['', 'amanha', 'reservar sala=x inicio=y fim=z', 'escolha=x'])
def test_invalid_booking(value):
    assert parse_booking(value) is None


def test_booking_and_choices():
    assert parse_booking(' reservar sala=x inicio=y fim=z responsavel=Doc Brown ') == {
        'sala': 'x', 'inicio': 'y', 'fim': 'z', 'responsavel': 'Doc Brown'}
    assert parse_choice('escolha=sala-mirante') == 'sala-mirante'
    assert parse_choice(' escolha=recusar ') == 'recusar'
    assert parse_choice('escolha=a b') is None


@pytest.mark.parametrize('value', ['', 'xx', '01-' + 'a'*32 + '-' + 'b'*16 + '-01',
    '00-' + '0'*32 + '-' + 'b'*16 + '-01', '00-' + 'a'*32 + '-' + '0'*16 + '-01'])
def test_invalid_trace(value):
    assert parse_traceparent(value) is None


def test_trace():
    trace = parse_traceparent('00-' + 'a'*32 + '-' + 'b'*16 + '-00')
    assert trace == Trace('a'*32, '00')
    first, second = trace.parent(), trace.parent()
    assert first != second and first.split('-')[1] == second.split('-')[1] == 'a'*32
    assert first.endswith('-00')
