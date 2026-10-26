"""Cliente MCP que entrega a decisão de elicitation ao chamador A2A."""

import os
from contextlib import asynccontextmanager
from itertools import count

from mcp.client import Client
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.types import ElicitResult, RequestParamsMeta, ClientCapabilities, ElicitationCapability, FormElicitationCapability

from agente.tracing import Trace

VERSION = '2026-07-28'


async def reject_automatic_elicitation(*args, **kwargs):
    raise RuntimeError('A escolha deve chegar pela continuação A2A')


class FormClient(Client):
    def __init__(self, *args, request_ids, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_ids = request_ids

    async def _build_session(self, exit_stack):
        """Adapta o anúncio do SDK 2.3.0 para somente a modalidade implementada."""
        session = await super()._build_session(exit_stack)
        # No SDK 2.3.0 o contador pertence ao dispatcher, não ao host.
        # Compartilha apenas a alocação; correlação e transporte continuam no SDK.
        if not isinstance(session._dispatcher, JSONRPCDispatcher):
            raise RuntimeError('Dispatcher MCP incompatível com a alocação de ids do host')
        session._dispatcher._allocate_id = self.request_ids.__next__
        # O SDK sobrescreve o _meta por chamada e anuncia form+url por padrão.
        # Este ponto roda antes de adopt(), que captura o anúncio da sessão.
        session._build_capabilities = lambda version: ClientCapabilities(
            elicitation=ElicitationCapability(form=FormElicitationCapability()))
        return session


class MCPHost:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get('MCP_URL', 'http://127.0.0.1:7301/mcp')
        self.tool_listing = None
        self.request_ids = count(1)

    def meta(self, trace: Trace) -> RequestParamsMeta:
        return {
            'traceparent': trace.parent(),
            'io.modelcontextprotocol/protocolVersion': VERSION,
            'io.modelcontextprotocol/clientCapabilities': {'elicitation': {'form': {}}},
        }

    @asynccontextmanager
    async def connect(self):
        async with FormClient(self.url, mode=VERSION, request_ids=self.request_ids,
                              elicitation_callback=reject_automatic_elicitation) as client:
            if self.tool_listing is not None:
                # Mantém a validação do SDK sem descoberta implícita e sem trace no retry.
                client.session._absorb_tool_listing(self.tool_listing, complete=True)
            yield client

    async def start(self, arguments: dict, trace: Trace):
        async with self.connect() as client:
            listing = await client.list_tools(meta=self.meta(trace), cache_mode='refresh')
            self.tool_listing = listing
            if 'reservar_sala' not in {tool.name for tool in listing.tools}:
                raise RuntimeError('Ferramenta reservar_sala ausente na descoberta')
            policy = await client.session.read_resource('politica://uso', meta=self.meta(trace))
            first_line = policy.contents[0].text.splitlines()[0]
            version = first_line.split(':', 1)[1].strip()
            result = await client.session.call_tool('reservar_sala', arguments, meta=self.meta(trace),
                                                    allow_input_required=True)
            return result, version

    async def resume(self, pending, choice: str, trace: Trace):
        answer = ElicitResult(action='decline') if choice == 'recusar' else ElicitResult(
            action='accept', content={'sala': choice})
        async with self.connect() as client:
            return await client.session.call_tool('reservar_sala', pending.arguments, meta=self.meta(trace),
                       input_responses={pending.input_key: answer}, request_state=pending.request_state,
                       allow_input_required=True)
