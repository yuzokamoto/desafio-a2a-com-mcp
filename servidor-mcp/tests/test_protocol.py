"""Reprodução do contrato HTTP e das rodadas MRTR."""

import asyncio
import copy
import json
import secrets
from pathlib import Path

import httpx
import pytest
from servidor_mcp.config import DATA_DIR
from servidor_mcp.server import create_app

ROOT = Path(__file__).resolve().parents[2]
TRACE = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'


def wire(number):
    return json.loads(next((ROOT / 'exemplos/wire').glob(f'{number:02d}-*.json')).read_text())['request']


class Harness:
    def __init__(self, client):
        self.client = client

    async def send(self, request):
        return await self.client.post('/mcp', json=request['body'], headers=request['headers'])

    async def conflict(self):
        request = wire(3)
        response = await self.send(request)
        assert response.status_code == 200
        result = response.json()['result']
        assert result['resultType'] == 'input_required', result
        return request, result

    async def retry(self, request, result, answer=None):
        request = copy.deepcopy(request)
        request['body']['id'] = secrets.token_hex(6)
        params = request['body']['params']
        params['requestState'] = result['requestState']
        params['inputResponses'] = {next(iter(result['inputRequests'])): answer or {'action': 'accept', 'content': {'sala': 'sala-fusca'}}}
        return await self.send(request)


def run(scenario):
    async def execute():
        app = create_app(secrets.token_bytes(32))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://localhost:17301') as client:
                await scenario(Harness(client))
    asyncio.run(execute())


def test_surface():
    async def scenario(h):
        response = await h.send(wire(1))
        assert response.status_code == 200
        result = response.json()['result']
        assert result['resultType'] == 'complete'
        tools = {tool['name']: tool for tool in result['tools']}
        assert set(tools) == {'listar_salas', 'consultar_disponibilidade', 'reservar_sala'}
        assert all(tool['inputSchema']['type'] == 'object' for tool in tools.values())
        response = await h.send(wire(2))
        result = response.json()['result']
        assert result['resultType'] == 'complete'
        assert result['structuredContent']['reserva'] == 'res-0003'
        assert json.loads(result['content'][0]['text']) == result['structuredContent']
        request = wire(2)
        request['body']['params'].update(name='listar_salas', arguments={})
        request['headers']['Mcp-Name'] = 'listar_salas'
        result = (await h.send(request)).json()['result']
        assert len(result['structuredContent']['salas']) == 5
        assert json.loads(result['content'][0]['text']) == result['structuredContent']
        response = await h.send(wire(5))
        content = response.json()['result']['contents'][0]
        assert content['text'] == (DATA_DIR / 'politica-de-uso.md').read_text()
        assert content['mimeType'] == 'text/markdown'
    run(scenario)


def test_protocol_errors():
    async def scenario(h):
        for field in ('protocolVersion', 'clientCapabilities'):
            request = wire(2)
            del request['body']['params']['_meta']['io.modelcontextprotocol/' + field]
            response = await h.send(request)
            assert response.status_code == 400
            assert response.json()['error']['code'] == -32602
        request = wire(5)
        request['body']['params']['uri'] = 'politica://inexistente'
        request['headers']['Mcp-Name'] = 'politica://inexistente'
        assert (await h.send(request)).json()['error']['code'] == -32602
        request = wire(2)
        request['body']['params']['name'] = request['headers']['Mcp-Name'] = 'voar_delorean'
        response = (await h.send(request)).json()
        assert response.get('error', {}).get('code') == -32602 or response.get('result', {}).get('isError')
        request = wire(2)
        request['headers']['Mcp-Name'] = 'nome-diferente'
        response = await h.send(request)
        assert response.status_code == 400 and response.json()['error']['code'] == -32020
    run(scenario)


def test_conflict_and_capability():
    async def scenario(h):
        _, result = await h.conflict()
        assert len(result['inputRequests']) == 1
        elicitation = next(iter(result['inputRequests'].values()))
        assert elicitation['method'] == 'elicitation/create'
        assert elicitation['params']['mode'] == 'form'
        assert elicitation['params']['requestedSchema']['properties']['sala']['enum'] == ['sala-fusca', 'sala-mirante']
        assert result['requestState'].startswith('v1.')
        response = await h.send(wire(6))
        assert response.status_code == 400
        assert response.json()['error']['code'] == -32021
        assert response.json()['error']['data']['requiredCapabilities'] == {'elicitation': {'form': {}}}
    run(scenario)


@pytest.mark.parametrize('action', ['accept', 'decline', 'cancel', 'outside', 'missing'])
def test_retry(action):
    async def scenario(h):
        request, result = await h.conflict()
        answer = {'action': action} if action in ('decline', 'cancel') else {'action': 'accept', 'content': {'sala': 'sala-aquario' if action == 'outside' else 'sala-fusca'}}
        if action == 'missing':
            request['body']['params'].update(requestState=result['requestState'], inputResponses={})
            response = await h.send(request)
            assert response.status_code == 400 and response.json()['error']['code'] == -32602
            return
        response = await h.retry(request, result, answer)
        output = response.json()['result']
        assert output['resultType'] == 'complete'
        if action == 'outside':
            assert output['isError'] and 'Escolha invalida' in output['content'][0]['text']
        elif action == 'accept':
            assert output['structuredContent']['sala'] == 'sala-fusca'
        else:
            assert output['structuredContent']['reservado'] is False and not output.get('isError')
    run(scenario)


def test_tamper_drift_expiry(monkeypatch):
    async def scenario(h):
        request, result = await h.conflict()
        tampered = dict(result, requestState=result['requestState'][:-6] + 'AAAAAA')
        response = await h.retry(request, tampered)
        assert response.status_code == 400 and response.json()['error']['code'] == -32602
        drift = copy.deepcopy(request)
        drift['body']['params']['arguments']['responsavel'] = 'Biff'
        response = await h.retry(drift, result)
        assert response.status_code == 400 and response.json()['error']['code'] == -32602
        import mcp.server.request_state as state_module
        original = state_module.time.time
        monkeypatch.setattr(state_module.time, 'time', lambda: original() + 601)
        response = await h.retry(request, result)
        assert response.status_code == 400 and response.json()['error']['code'] == -32602
    run(scenario)


def test_taken_and_no_alternatives():
    async def scenario(h):
        request, result = await h.conflict()
        free = wire(2)
        free['body']['params']['arguments'] = dict(sala='sala-fusca', inicio='2026-11-03T14:00:00-03:00', fim='2026-11-03T15:00:00-03:00', responsavel='Doc')
        assert (await h.send(free)).json()['result']['structuredContent']['reservado']
        output = (await h.retry(request, result)).json()['result']
        assert output['resultType'] == 'input_required'
        assert next(iter(output['inputRequests'].values()))['params']['requestedSchema']['properties']['sala']['enum'] == ['sala-mirante']
        free['body']['params']['arguments']['sala'] = 'sala-mirante'
        await h.send(free)
        output = (await h.retry(request, result)).json()['result']
        assert output['isError'] and output['content'][0]['text'] == 'Sem alternativas disponiveis no intervalo'
    run(scenario)


def test_restart():
    async def scenario():
        secret = secrets.token_bytes(32)
        first = create_app(secret)
        async with first.router.lifespan_context(first):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(first), base_url='http://localhost:17301') as client:
                request, result = await Harness(client).conflict()
        second = create_app(secret)
        async with second.router.lifespan_context(second):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(second), base_url='http://localhost:17301') as client:
                output = (await Harness(client).retry(request, result)).json()['result']
                assert output['structuredContent']['sala'] == 'sala-fusca'
    asyncio.run(scenario())


def test_log(capsys):
    async def scenario(h):
        request, result = await h.conflict()
        await h.retry(request, result)
        request = wire(2)
        del request['body']['params']['_meta']['io.modelcontextprotocol/protocolVersion']
        await h.send(request)
        log = capsys.readouterr().err
        for field in ('tools/call', 'reservar_sala', TRACE, 'input_required', '-32602', 'requestState="yes"', 'id=3'):
            assert field in log
        assert result['requestState'] not in log
    run(scenario)


@pytest.mark.parametrize(('field', 'shape'), [
    ('params', 'scalar'), ('params', 'list'), ('params', 'empty-list'),
    ('_meta', 'scalar'), ('_meta', 'list'), ('_meta', 'empty-list'),
    ('root', 'scalar'), ('root', 'string'), ('root', 'list'),
    ('root', 'empty-list'), ('root', 'null'),
])
def test_log_malformed_request(field, shape, capsys):
    async def scenario():
        secret = secrets.token_bytes(32)
        token = 'v1.' + secrets.token_urlsafe(32)
        private_value = secrets.token_hex(32)
        invalid = {'scalar': 42, 'string': private_value, 'list': [private_value],
                   'empty-list': [], 'null': None}[shape]
        request = wire(1)
        request_id = 'malformed-request'
        request['body'].update(id=request_id, private=private_value)
        if field == 'root':
            request['body'] = invalid
        elif field == 'params':
            request['body']['params'] = invalid
        else:
            request['body']['params'].update(_meta=invalid, requestState=token)

        app = create_app(secret)
        async with app.router.lifespan_context(app):
            # O transporte propaga exceções ASGI, inclusive depois de enviar a resposta.
            transport = httpx.ASGITransport(app, raise_app_exceptions=True)
            async with httpx.AsyncClient(transport=transport, base_url='http://localhost:17301') as client:
                response = await client.post('/mcp', content=json.dumps(request['body']), headers=request['headers'])
        expected_code = -32602 if field == '_meta' else -32600
        assert response.status_code == 400
        assert response.json()['error']['code'] == expected_code
        assert response.json()['id'] == (request_id if field == '_meta' else None)

        log = capsys.readouterr().err
        lines = [line for line in log.splitlines() if line.startswith('mcp ')]
        expected = dict(method='?' if field == 'root' else 'tools/list',
                        id=None if field == 'root' else request_id,
                        name='', traceparent='', requestState='yes' if field == '_meta' else 'no',
                        http=400, outcome=f'error={expected_code}')
        assert lines == ['mcp ' + ' '.join(f'{key}={json.dumps(value)}' for key, value in expected.items())]
        assert 'Traceback' not in log
        assert token not in log and private_value not in log
        assert secret.hex() not in log and repr(secret) not in log

    asyncio.run(scenario())
