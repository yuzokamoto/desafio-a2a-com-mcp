"""Servidor MCP nativo com reservas e ciclo de escolha de alternativa."""

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import MCPServer, Context
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, InputRequiredResult, ElicitRequest, ElicitRequestFormParams, TextContent

from servidor_mcp.config import DATA_DIR, STATE_TTL, load_secret
from servidor_mcp.domain import BookingDomain, NO_ALTERNATIVES
from servidor_mcp.request_log import RequestLog
from servidor_mcp.models import RoomList, Availability, Booking


def complete(data: dict) -> CallToolResult:
    return CallToolResult(structured_content=data, content=[TextContent(type='text', text=json.dumps(data))])


def failure(message: str) -> CallToolResult:
    return CallToolResult(is_error=True, content=[TextContent(type='text', text=message)])


def create_app(secret: bytes | None = None, data_dir: Path | None = None):
    domain = BookingDomain(data_dir or Path(os.environ.get('DADOS_DIR', str(DATA_DIR))))
    server = MCPServer('central-de-salas', version='1.0.0',
                       request_state_security=RequestStateSecurity(keys=[secret if secret is not None else load_secret()],
                                                                   ttl=STATE_TTL))

    @server.tool()
    def listar_salas() -> Annotated[CallToolResult, RoomList]:
        """Lista todas as salas com capacidade e recursos."""
        return complete({'salas': domain.rooms})

    @server.tool()
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Annotated[CallToolResult, Availability]:
        """Consulta conflitos e disponibilidade no intervalo informado."""
        try:
            conflicts = domain.conflicts(sala, inicio, fim)
            return complete(dict(sala=sala, livre=not conflicts, conflitos=conflicts))
        except ValueError as exc:
            return failure(str(exc))

    def conflict(arguments: dict, ctx: Context) -> CallToolResult | InputRequiredResult:
        options = domain.alternatives(arguments['sala'], arguments['inicio'], arguments['fim'])
        if not options:
            return failure(NO_ALTERNATIVES)
        capabilities = ctx.client_capabilities
        if capabilities is None or capabilities.elicitation is None or capabilities.elicitation.form is None:
            raise MCPError(-32021, 'Cliente nao declarou elicitation.form',
                           data={'requiredCapabilities': {'elicitation': {'form': {}}}})
        key = 'escolha_de_sala'
        schema = {'type': 'object', 'properties': {'sala': {'type': 'string', 'title': 'Sala',
                  'description': 'Sala alternativa escolhida', 'enum': options}}, 'required': ['sala']}
        return InputRequiredResult(input_requests={key: ElicitRequest(method='elicitation/create',
                                   params=ElicitRequestFormParams(mode='form',
                                   message='A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.',
                                   requested_schema=schema))},
                                   request_state=json.dumps(dict(v=1, pedido=arguments, alternativas=options, chave=key)))

    @server.tool()
    def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str,
                      ctx: Context) -> Annotated[CallToolResult, Booking] | InputRequiredResult:
        """Reserva uma sala ou solicita a escolha de uma alternativa."""
        arguments = dict(sala=sala, inicio=inicio, fim=fim, responsavel=responsavel)
        try:
            if ctx.request_state is not None:
                state = json.loads(ctx.request_state)
                arguments = state['pedido']
                answer = (ctx.input_responses or {}).get(state['chave'])
                if answer is None:
                    raise MCPError(-32602, 'Resposta de escolha ausente')
                if answer.action in ('decline', 'cancel'):
                    return complete(dict(reserva=None, reservado=False, sala=None, inicio=None, fim=None,
                                         responsavel=None, politica=None,
                                         motivo='recusado' if answer.action == 'decline' else 'cancelado'))
                choice = (answer.content or {}).get('sala')
                if choice not in state['alternativas']:
                    return failure(f'Escolha invalida: {choice} nao esta entre as alternativas oferecidas')
                if domain.conflicts(choice, arguments['inicio'], arguments['fim']):
                    return conflict(arguments, ctx)
                return complete(domain.book(choice, arguments['inicio'], arguments['fim'], arguments['responsavel']))
            domain.validate(sala, inicio, fim)
            if domain.conflicts(sala, inicio, fim):
                return conflict(arguments, ctx)
            return complete(domain.book(sala, inicio, fim, responsavel))
        except ValueError as exc:
            return failure(str(exc))

    @server.resource('politica://uso', mime_type='text/markdown')
    def usage_policy() -> str:
        """Política de uso carregada do arquivo do desafio."""
        return domain.policy

    app = server.streamable_http_app(stateless_http=True, json_response=True)
    app.add_middleware(RequestLog)
    return app


def main():
    try:
        app = create_app()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(app, host=os.environ.get('MCP_HOST', '127.0.0.1'), port=int(os.environ.get('MCP_PORT', '7301')))
