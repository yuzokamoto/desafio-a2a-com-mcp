"""Montagem do servidor A2A v1.0 com as rotas oficiais do SDK."""

import os

import uvicorn
from starlette.applications import Starlette
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes, DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.agent_execution.simple_request_context_builder import SimpleRequestContextBuilder
from a2a.types.a2a_pb2 import AgentCard, AgentCapabilities, AgentInterface, AgentProvider, AgentSkill

from agente.bridge import BookingExecutor


class V1ContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        """Usa v1.0 neste endpoint quando o cliente do desafio omite o header."""
        context = super().build(request)
        context.state['headers'].setdefault('a2a-version', '1.0')
        return context


class HistoryTaskStore(InMemoryTaskStore):
    async def save(self, task, context):
        """Inclui também a mensagem corrente no histórico, sem duplicação."""
        seen = set()
        messages = []
        for message in [*task.history, *([task.status.message] if task.status.HasField('message') else [])]:
            if message.message_id not in seen:
                seen.add(message.message_id)
                messages.append(message)
        del task.history[:]
        task.history.extend(messages)
        await super().save(task, context)


class StableTaskContextBuilder(SimpleRequestContextBuilder):
    async def build(self, context, params=None, task_id=None, context_id=None, task=None):
        """Reutiliza o contexto salvo quando a continuação traz somente taskId."""
        # O handler V2 passa task=None mesmo após consultar a tarefa existente.
        if task is None and task_id and not context_id:
            task = await self._task_store.get(task_id, context)
        if task is not None and not context_id:
            context_id = task.context_id
        return await super().build(context=context, params=params, task_id=task_id,
                                   context_id=context_id, task=task)


def create_app(host=None):
    public_url = os.environ.get('AGENT_PUBLIC_URL', 'http://localhost:' + os.environ.get('AGENT_PORT', '7300'))
    card = AgentCard(name='Central de Salas', description='Reserva salas de reuniao da Hill Valley Tech.',
        provider=AgentProvider(organization='Hill Valley Tech', url='https://hillvalley.example'), version='1.0.0',
        supported_interfaces=[AgentInterface(url=public_url.rstrip('/') + '/a2a',
                                             protocol_binding='JSONRPC', protocol_version='1.0')],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=['text/plain'], default_output_modes=['text/plain'],
        skills=[AgentSkill(id='reservar-sala', name='Reservar sala',
            description='Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.',
            tags=['salas', 'agenda'], input_modes=['text/plain'], output_modes=['text/plain'],
            examples=['reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty'])])
    executor = BookingExecutor(host)
    store = HistoryTaskStore()
    handler = DefaultRequestHandler(agent_executor=executor, task_store=store, agent_card=card,
                                    request_context_builder=StableTaskContextBuilder(task_store=store))
    app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, '/a2a', context_builder=V1ContextBuilder())])
    app.state.executor = executor
    return app


def main():
    uvicorn.run(create_app(), host=os.environ.get('AGENT_HOST', '127.0.0.1'),
                port=int(os.environ.get('AGENT_PORT', '7300')))
