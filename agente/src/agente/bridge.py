"""Ponte entre a pausa MCP e o ciclo de vida das tarefas A2A."""

import asyncio
import json
from dataclasses import dataclass, field

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Part, Task, TaskStatus, TaskState
from mcp.types import InputRequiredResult

from agente.grammar import INVALID_REQUEST, parse_booking, parse_choice
from agente.mcp_host import MCPHost
from agente.tracing import Trace, new_trace, parse_traceparent


@dataclass
class PendingCall:
    arguments: dict
    input_key: str
    options: list[str]
    request_state: str = field(repr=False)
    policy_version: str
    trace: Trace

    @property
    def prompt(self):
        return 'alternativas: ' + ', '.join(self.options)


class BookingExecutor(AgentExecutor):
    def __init__(self, host: MCPHost | None = None):
        self.host = host or MCPHost()
        self.pending: dict[str, PendingCall] = {}
        self.lock = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue):
        # O lock preserva o vínculo da pausa durante uma continuação.
        async with self.lock:
            await self._execute(context, event_queue)

    async def _execute(self, context: RequestContext, event_queue):
        task_id, context_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)
        message = lambda text: updater.new_agent_message([Part(text=text)])
        pending = self.pending.get(task_id)
        incoming = parse_traceparent(context.call_context.state.get('headers', {}).get('traceparent'))
        trace = incoming or (pending.trace if pending else new_trace())
        if context.current_task is None:
            await event_queue.enqueue_event(Task(id=task_id, context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message] if context.message else []))
            await updater.submit()
        await updater.start_work()
        text = context.get_user_input()
        if pending:
            choice = parse_choice(text)
            if choice != 'recusar' and choice not in pending.options:
                await updater.requires_input(message(pending.prompt))
                return
            arguments, policy_version = pending.arguments, pending.policy_version
        else:
            arguments = parse_booking(text)
            if arguments is None:
                await updater.reject(message(INVALID_REQUEST))
                return
        try:
            if pending:
                result = await self.host.resume(pending, choice, trace)
            else:
                result, policy_version = await self.host.start(arguments, trace)
            await self.apply_result(result, arguments, policy_version, trace, task_id, updater)
        except Exception:
            # Exceções de transporte podem conter o corpo: não repassamos estado opaco.
            self.pending.pop(task_id, None)
            await updater.failed(message('Falha ao falar com o servidor MCP: chamada indisponivel ou resposta invalida'))

    async def apply_result(self, result, arguments, policy_version, trace, task_id, updater):
        """Converte input_required em pausa; guarda o estado somente no agente."""
        message = lambda text: updater.new_agent_message([Part(text=text)])
        if isinstance(result, InputRequiredResult):
            key, request = next(iter(result.input_requests.items()))
            schema = request.params.requested_schema['properties']['sala']
            options = schema.get('enum') or [schema['const']]
            pending = PendingCall(arguments, key, options, result.request_state, policy_version, trace)
            self.pending[task_id] = pending
            await updater.requires_input(message(pending.prompt))
            return
        self.pending.pop(task_id, None)
        if result.is_error:
            await updater.failed(message(' '.join(block.text for block in result.content if block.type == 'text')))
            return
        data = result.structured_content
        if not data['reservado']:
            await updater.cancel(message('Reserva recusada: nenhuma sala foi reservada.'))
            return
        artifact = {key: data[key] for key in ('reserva', 'sala', 'inicio', 'fim', 'responsavel')}
        artifact['politica'] = policy_version
        await updater.add_artifact([Part(text=json.dumps(artifact))], name='reserva')
        await updater.complete(message(f"Reserva {data['reserva']} confirmada na {data['sala']}."))

    async def cancel(self, context: RequestContext, event_queue):
        async with self.lock:
            self.pending.pop(context.task_id, None)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()
