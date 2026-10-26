"""Integração da ponte com processo MCP real e captura do transporte."""

import asyncio
import copy
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from agente.app import create_app
from agente.mcp_host import MCPHost

ROOT = Path(__file__).resolve().parents[2]
TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736'
TRACE = f'00-{TRACE_ID}-00f067aa0ba902b7-01'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ServerProcess:
    def __init__(self, log):
        self.port = free_port()
        self.url = f'http://127.0.0.1:{self.port}/mcp'
        self.env = {**os.environ, 'REQUEST_STATE_SECRET': secrets.token_hex(32), 'MCP_PORT': str(self.port), 'MCP_HOST': '127.0.0.1'}
        self.log = log
        self.process = None

    def start(self):
        self.process = subprocess.Popen([str(ROOT/'servidor-mcp/.venv/bin/servidor-mcp')],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('Processo MCP encerrou durante a inicialização')
            try:
                urllib.request.urlopen(self.url, timeout=0.5).close()
                return
            except urllib.error.HTTPError:
                return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError('Tempo esgotado ao iniciar MCP')

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.fixture
def server(tmp_path):
    with (tmp_path/'mcp.log').open('w+') as log:
        server = ServerProcess(log)
        try:
            server.start()
            yield server
        finally:
            server.stop()


@pytest.fixture
def recorded(server):
    records = []
    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            records.append((json.loads(body), {key.lower(): value for key, value in self.headers.items()}))
            headers = {key: value for key, value in self.headers.items() if key.lower() not in ('host', 'content-length', 'connection')}
            request = urllib.request.Request(server.url, data=body, headers=headers, method='POST')
            try:
                response = urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                payload = response.read()
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        def log_message(self, *args):
            pass
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{proxy.server_port}/mcp', records
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)


class A2AClient:
    def __init__(self, client):
        self.client = client
        self.responses = []

    async def rpc(self, method, params, traced=False):
        response = await self.client.post('/a2a', json=dict(jsonrpc='2.0', id=secrets.token_hex(6), method=method, params=params),
                                          headers={'traceparent': TRACE} if traced else {})
        assert response.status_code == 200
        data = response.json()
        self.responses.append(data)
        return data

    async def send(self, text, task=None, traced=False):
        message = dict(messageId=secrets.token_hex(6), role='ROLE_USER', parts=[{'text': text}])
        if task:
            message['taskId'] = task['id']
        response = await self.rpc('SendMessage', {'message': message}, traced)
        return response.get('result', {}).get('task', response)


def booking(room='sala-garagem', start='14:00', end='15:00'):
    return f'reservar sala={room} inicio=2026-11-03T{start}:00-03:00 fim=2026-11-03T{end}:00-03:00 responsavel=Doc'


def state(task):
    return task['status']['state']


def text(task):
    return task['status']['message']['parts'][0]['text']


def artifact(task):
    assert task['artifacts'][0]['name'] == 'reserva'
    return json.loads(task['artifacts'][0]['parts'][0]['text'])


def run_flow(url, scenario):
    async def execute():
        app = create_app(MCPHost(url))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://localhost:17300') as client:
            await scenario(A2AClient(client), app)
    asyncio.run(execute())


def test_card_and_free(recorded):
    url, records = recorded
    async def scenario(client, app):
        response = await client.client.get('/.well-known/agent-card.json')
        assert response.status_code == 200
        card = response.json()
        example = json.loads(next((ROOT/'exemplos/wire').glob('07-*.json')).read_text())['response']['body']
        assert set(example) <= set(card)
        assert card['supportedInterfaces'][0]['protocolBinding'] == 'JSONRPC'
        assert card['supportedInterfaces'][0]['protocolVersion'] == '1.0'
        assert card['supportedInterfaces'][0]['url'].endswith('/a2a')
        assert card['skills'][0]['id'] == 'reservar-sala'
        task = await client.send(booking('sala-porao', '09:00', '10:00'), traced=True)
        assert state(task) == 'TASK_STATE_COMPLETED', task
        assert artifact(task)['politica'] == '2026-11-01'
        got = await client.rpc('GetTask', {'id': task['id']})
        assert got['result'].get('task', got['result'])['id'] == task['id'] and task['contextId']
        assert task['status']['message'] in task['history']
        terminal = await client.send('escolha=sala-mirante', task)
        assert 'error' in terminal
        assert not app.state.executor.pending
    run_flow(url, scenario)
    assert [body['method'] for body, _ in records][:3] == ['tools/list', 'resources/read', 'tools/call']


def test_pause_retry_wire_trace_and_privacy(recorded, server):
    url, records = recorded
    tokens = []
    async def scenario(client, app):
        task = await client.send(booking(), traced=True)
        assert state(task) == 'TASK_STATE_INPUT_REQUIRED', task
        assert text(task) == 'alternativas: sala-fusca, sala-mirante'
        pending = app.state.executor.pending[task['id']]
        tokens.append(pending.request_state)
        assert len(records) == 3
        count = len(records)
        mismatch = await client.rpc('SendMessage', {'message': {
            'messageId': secrets.token_hex(6), 'role': 'ROLE_USER',
            'taskId': task['id'], 'contextId': 'different-context',
            'parts': [{'text': 'escolha=sala-mirante'}]}})
        assert 'error' in mismatch and len(records) == count
        for choice in ('escolha=sala-aquario', 'escolha=inexistente', 'texto invalido'):
            invalid = await client.send(choice, task)
            assert state(invalid) == 'TASK_STATE_INPUT_REQUIRED' and len(records) == count
            assert (invalid['id'], invalid['contextId']) == (task['id'], task['contextId'])
            assert text(invalid) == text(task)
        got = await client.rpc('GetTask', {'id': task['id']})
        assert state(got['result'].get('task', got['result'])) == 'TASK_STATE_INPUT_REQUIRED'
        complete = await client.send('escolha=sala-mirante', task)
        assert state(complete) == 'TASK_STATE_COMPLETED', complete
        assert (complete['id'], complete['contextId']) == (task['id'], task['contextId'])
        assert artifact(complete)['sala'] == 'sala-mirante'
        assert task['id'] not in app.state.executor.pending
        encoded = json.dumps(client.responses)
        assert all(token not in encoded for token in tokens)
        assert 'requestState' not in encoded
    run_flow(url, scenario)
    calls = [body for body, _ in records if body['method'] == 'tools/call']
    assert len(calls) == 2 and calls[0]['id'] != calls[1]['id']
    assert calls[0]['params']['arguments'] == calls[1]['params']['arguments']
    assert calls[1]['params']['requestState'] == tokens[0]
    assert calls[1]['params']['inputResponses'] == {'escolha_de_sala': {'action': 'accept', 'content': {'sala': 'sala-mirante'}}}
    parents = []
    for body, headers in records:
        meta = body['params']['_meta']
        assert meta['io.modelcontextprotocol/protocolVersion'] == '2026-07-28'
        assert meta['io.modelcontextprotocol/clientCapabilities'] == {'elicitation': {'form': {}}}
        assert headers['mcp-protocol-version'] == '2026-07-28'
        assert headers['mcp-method'] == body['method']
        if body['method'] in ('tools/call', 'resources/read'):
            assert headers['mcp-name'] == body['params'].get('name', body['params'].get('uri'))
        assert meta['traceparent'].split('-')[1] == TRACE_ID
        parents.append(meta['traceparent'])
    assert len(set(parents)) == len(parents)
    server.log.flush()
    server.log.seek(0)
    log = server.log.read()
    assert TRACE_ID in log and tokens[0] not in log
    assert log.index('tools/list') < log.index('tools/call')


def test_refusal_failure_rejection_and_concurrent_pauses(recorded):
    url, records = recorded
    async def scenario(client, app):
        rejected = await client.send('quero uma sala amanha')
        assert state(rejected) == 'TASK_STATE_REJECTED' and not records
        failed = await client.send(booking('sala-delorean'))
        assert state(failed) == 'TASK_STATE_FAILED' and text(failed) == 'Sala inexistente: sala-delorean'
        assert failed['status']['message'] in failed['history']
        paused = await client.send(booking())
        canceled = await client.send('escolha=recusar', paused)
        assert state(canceled) == 'TASK_STATE_CANCELED'
        assert canceled['contextId'] == paused['contextId']
        for terminal in (rejected, failed, canceled):
            assert 'error' in await client.send('escolha=sala-mirante', terminal)
            got = await client.rpc('GetTask', {'id': terminal['id']})
            saved = got['result'].get('task', got['result'])
            assert (state(saved), saved['contextId']) == (state(terminal), terminal['contextId'])
        first, second = await asyncio.gather(client.send(booking('sala-fusca', '16:00', '17:00')), client.send(booking()))
        assert state(first) == state(second) == 'TASK_STATE_INPUT_REQUIRED'
        assert len(app.state.executor.pending) == 2
        tokens = [entry.request_state for entry in app.state.executor.pending.values()]
        a = await client.send('escolha=sala-mirante', first)
        b = await client.send('escolha=sala-mirante', second)
        assert state(a) == state(b) == 'TASK_STATE_COMPLETED'
        assert artifact(a)['reserva'] != artifact(b)['reserva']
        assert artifact(a)['inicio'] != artifact(b)['inicio']
        assert not app.state.executor.pending
        assert all(token not in json.dumps(client.responses) for token in tokens)
    run_flow(url, scenario)
    ids = [body['id'] for body, _ in records]
    assert len(ids) == len(set(ids))


def test_repeated_retry_and_independent_tasks_have_unique_ids(recorded, server):
    url, records = recorded
    tokens = []
    async def scenario(client, app):
        task = await client.send(booking(), traced=True)
        other = await client.send(booking())
        assert task['id'] != other['id']
        assert state(task) == state(other) == 'TASK_STATE_INPUT_REQUIRED'
        assert text(task) == text(other) == 'alternativas: sala-fusca, sala-mirante'
        tokens.extend(entry.request_state for entry in app.state.executor.pending.values())

        # Um chamador externo ocupa a alternativa após ela ter sido oferecida.
        external = copy.deepcopy(records[2][0])
        external['id'] = 'external-occupancy'
        external['params']['arguments']['sala'] = 'sala-fusca'
        async with httpx.AsyncClient() as outsider:
            response = await outsider.post(server.url, json=external, headers={
                'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': 'tools/call',
                'Mcp-Name': 'reservar_sala'})
        assert response.status_code == 200
        assert response.json()['result']['structuredContent']['reservado'] is True

        paused = await client.send('escolha=sala-fusca', task)
        assert state(paused) == 'TASK_STATE_INPUT_REQUIRED'
        assert text(paused) == 'alternativas: sala-mirante'
        tokens.append(app.state.executor.pending[task['id']].request_state)
        assert tokens[-1] != tokens[0]
        complete = await client.send('escolha=sala-mirante', paused)
        assert state(complete) == 'TASK_STATE_COMPLETED'
        assert artifact(complete)['sala'] == 'sala-mirante'
        for result in (paused, complete):
            assert (result['id'], result['contextId']) == (task['id'], task['contextId'])
        canceled = await client.send('escolha=recusar', other)
        assert state(canceled) == 'TASK_STATE_CANCELED'
        assert (canceled['id'], canceled['contextId']) == (other['id'], other['contextId'])
        got = await client.rpc('GetTask', {'id': task['id']})
        assert state(got['result'].get('task', got['result'])) == 'TASK_STATE_COMPLETED'
        assert not app.state.executor.pending
        encoded = json.dumps(client.responses)
        assert 'requestState' not in encoded
        assert all(token not in encoded for token in tokens)
    run_flow(url, scenario)

    assert [body['method'] for body, _ in records] == [
        'tools/list', 'resources/read', 'tools/call',
        'tools/list', 'resources/read', 'tools/call',
        'tools/call', 'tools/call', 'tools/call']
    ids = [body['id'] for body, _ in records]
    assert len(ids) == len(set(ids))
    rounds = [records[index][0] for index in (2, 6, 7)]
    assert len({body['id'] for body in rounds}) == 3
    assert all(body['params']['arguments'] == rounds[0]['params']['arguments'] for body in rounds)
    assert rounds[1]['params']['requestState'] == tokens[0]
    assert rounds[2]['params']['requestState'] == tokens[-1]
    assert records[8][0]['params']['requestState'] == tokens[1]
    parents = []
    for index, (body, headers) in enumerate(records):
        meta = body['params']['_meta']
        assert meta['io.modelcontextprotocol/protocolVersion'] == headers['mcp-protocol-version'] == '2026-07-28'
        assert meta['io.modelcontextprotocol/clientCapabilities'] == {'elicitation': {'form': {}}}
        assert headers['mcp-method'] == body['method']
        if body['method'] != 'tools/list':
            assert headers['mcp-name'] == body['params'].get('name', body['params'].get('uri'))
        if index in (0, 1, 2, 6, 7):
            assert meta['traceparent'].split('-')[1] == TRACE_ID
        parents.append(meta['traceparent'])
    assert len(parents) == len(set(parents))
    server.log.flush()
    server.log.seek(0)
    log = server.log.read()
    assert all(token not in log for token in tokens)
    for body in rounds:
        lines = [line for line in log.splitlines() if f' id={json.dumps(body["id"])} ' in line]
        assert len(lines) == 1
        assert 'method="tools/call"' in lines[0] and TRACE_ID in lines[0]


def test_mcp_restart_tamper_capability(server):
    request = json.loads(next((ROOT/'exemplos/wire').glob('03-*.json')).read_text())['request']
    request['body']['params']['_meta']['traceparent'] = TRACE
    def post(request):
        with httpx.Client() as client:
            return client.post(server.url, json=request['body'], headers=request['headers'])
    result = post(request).json()['result']
    server.stop()
    server.start()
    retry = copy.deepcopy(request)
    retry['body']['id'] = 'retry-after-restart'
    retry['body']['params'].update(requestState=result['requestState'], inputResponses={
        next(iter(result['inputRequests'])): {'action': 'accept', 'content': {'sala': 'sala-fusca'}}})
    response = post(retry)
    assert response.status_code == 200
    assert response.json()['result']['structuredContent']['sala'] == 'sala-fusca'
    retry['body']['params']['requestState'] = result['requestState'][:-6] + 'AAAAAA'
    response = post(retry)
    assert response.status_code == 400 and response.json()['error']['code'] == -32602
    request['body']['params']['_meta']['io.modelcontextprotocol/clientCapabilities'] = {}
    response = post(request)
    assert response.status_code == 400 and response.json()['error']['code'] == -32021
    server.log.flush()
    server.log.seek(0)
    log = server.log.read()
    assert TRACE_ID in log and result['requestState'] not in log
    assert 'id="retry-after-restart"' in log and 'requestState="yes"' in log
    assert 'error=-32021' in log and 'http=400' in log
    print(f'Reinício/retry, tamper e capability: PASS; MCP={server.port}; encerramento no finally')
