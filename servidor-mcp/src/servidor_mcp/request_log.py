"""Registro por requisição, sem expor o estado opaco."""

import json
import sys


class RequestLog:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or scope['path'] != '/mcp':
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            body.extend(event.get('body', b''))
            if not event.get('more_body'):
                break
        try:
            request = json.loads(body)
        except (ValueError, TypeError):
            request = {}
        status = 500
        response = bytearray()
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body)}
            return await receive()

        async def capture(event):
            nonlocal status
            if event['type'] == 'http.response.start':
                status = event['status']
            elif event['type'] == 'http.response.body':
                response.extend(event.get('body', b''))
            await send(event)

        try:
            await self.app(scope, replay, capture)
        finally:
            try:
                result = json.loads(response)
                outcome = ('error=' + str(result['error']['code']) if 'error' in result else
                           result.get('result', {}).get('resultType', '?'))
                if result.get('result', {}).get('isError'):
                    outcome += ' isError=true'
            except (ValueError, TypeError):
                outcome = '?'
            for item in request if isinstance(request, list) and request else [request]:
                item = item if isinstance(item, dict) else {}
                params = item.get('params')
                params = params if isinstance(params, dict) else {}
                meta = params.get('_meta')
                meta = meta if isinstance(meta, dict) else {}
                # A serialização escapa quebras de linha vindas do cliente.
                fields = dict(method=item.get('method', '?'), id=item.get('id'),
                              name=params.get('name', params.get('uri', '')),
                              traceparent=meta.get('traceparent', ''),
                              requestState='yes' if 'requestState' in params else 'no',
                              http=status, outcome=outcome)
                print('mcp ' + ' '.join(f'{key}={json.dumps(value)}' for key, value in fields.items()),
                      file=sys.stderr, flush=True)
