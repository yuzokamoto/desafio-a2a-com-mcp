# A Ponte: A2A + MCP

Agente determinístico de reservas, sem LLM: recebe mensagens A2A v1.0 e atua como host MCP sobre HTTP. O servidor MCP concentra as regras de salas e reservas.

## Como rodar

Pré-requisitos: `uv`, Python 3.12+ e `python3` disponíveis. Execute os comandos na raiz deste repositório. Cada projeto tem ambiente e lock próprios.

```bash
uv sync --locked --project servidor-mcp
uv sync --locked --project agente
```

No terminal 1, gere uma chave efêmera, sem exibir seu valor, e inicie o MCP:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
uv run --locked --project servidor-mcp servidor-mcp
```

Mantenha essa variável no terminal para reiniciar o MCP com a mesma chave. Uma chave ausente ou com menos de 32 bytes impede a inicialização. Não é necessário arquivo `.env`, credencial externa ou serviço de modelo.

No terminal 2, também na raiz:

```bash
uv run --locked --project agente agente
```

No terminal 3, com os dois processos recém-iniciados:

```bash
python3 validador/validar.py --agente http://127.0.0.1:7300 --mcp http://127.0.0.1:7301
```

Os endereços padrão são `http://127.0.0.1:7301/mcp`, `http://127.0.0.1:7300/a2a` e `http://127.0.0.1:7300/.well-known/agent-card.json`. Encerre os processos com Ctrl+C. Reservas acumulam durante a execução: reinicie ambos antes de repetir o validador.

Para executar toda a validação em processos novos e encerrar ambos automaticamente, use o harness nas portas de teste:

```bash
MCP_PORT=17301 AGENT_PORT=17300 bash scripts/validar-local.sh
```

O harness gera a chave em memória quando ausente, configura as URLs e salva saída e stderr em `.runtime-logs/` (`LOG_DIR` permite outro diretório). Preserva o código de saída do validador. As portas escolhidas precisam estar livres. O servidor aceita `MCP_HOST`, `MCP_PORT` e `DADOS_DIR`; o agente aceita `AGENT_HOST`, `AGENT_PORT`, `MCP_URL` e `AGENT_PUBLIC_URL` (base pública sem `/a2a`). Para portas manuais diferentes, ajuste também as URLs no agente e no validador.

Testes, incluindo subprocessos MCP em portas livres e reinício com a mesma chave:

```bash
uv run --project servidor-mcp pytest -q
uv run --project agente pytest -q
uv run --project agente pytest -q -s agente/tests/test_integration.py::test_mcp_restart_tamper_capability
```

## Onde a ponte acontece

Em [`BookingExecutor.apply_result`](agente/src/agente/bridge.py), um `InputRequiredResult` do SDK MCP vira `TaskUpdater.requires_input`, que publica `TASK_STATE_INPUT_REQUIRED`. O `PendingCall`, indexado pelo id da Task em `BookingExecutor.pending`, guarda os argumentos originais, a chave de input, o enum oferecido, a versão da política, o trace e o `requestState` opaco. A mensagem pública é exatamente `alternativas: ...`.

Em [`BookingExecutor._execute`](agente/src/agente/bridge.py), uma continuação `escolha=<id>` valida somente se o id estava no enum; escolha inválida repete a pausa sem chamada MCP. `escolha=recusar` é encaminhado como `decline`. A chamada [`MCPHost.resume`](agente/src/agente/mcp_host.py) repete `reservar_sala` com os mesmos argumentos, a mesma chave em `inputResponses`, o `requestState` byte a byte e um novo id JSON-RPC. O agente não abre o token nem decide conflitos ou regras de uso.

[`MCPHost.start`](agente/src/agente/mcp_host.py) faz `tools/list` por nova Task, exige a descoberta de `reservar_sala`, lê `politica://uso` e chama a ferramenta. A versão lida da primeira linha da política compõe o artifact `reserva`. [`reservar_sala`, dentro de `create_app`](servidor-mcp/src/servidor_mcp/server.py), implementa as rodadas MCP; [`BookingDomain`](servidor-mcp/src/servidor_mcp/domain.py) aplica as regras do domínio.

## Decisões técnicas

São dois processos Python 3.12 independentes com dependências diretas fixadas por `==` e respectivos `uv.lock`: `mcp==2.3.0` nos dois e `a2a-sdk[http-server]==1.2.3` no agente. Não há dependência de provedor LLM nem chamada paga. `dados/`, `validador/` e `exemplos/` permanecem intactos.

O servidor usa `MCPServer.streamable_http_app(stateless_http=True, json_response=True)`: respostas JSON no endpoint `/mcp`, sem sessão de protocolo. `RequestStateSecurity` do SDK sela o pedido completo e as alternativas com AES-GCM, validade de 600 segundos e vínculo ao método, ferramenta, argumentos e servidor. O segredo vem de `REQUEST_STATE_SECRET`: hexadecimal é decodificado; outros valores são interpretados como UTF-8. O SDK rejeita adulteração, expiração e troca de argumentos com `-32602`. O estado permite retry após reinício com a mesma chave. As reservas são somente em memória e retornam aos dados iniciais no reinício.

Tasks, histórico e artifacts vivem no `HistoryTaskStore`, derivado de `InMemoryTaskStore` do SDK A2A; pausas vivem no dicionário do executor, protegido por lock. Reiniciar o agente perde Tasks e pausas. O `requestState` nunca vai para status, histórico, artifact, card ou erro A2A. O middleware [`RequestLog`](servidor-mcp/src/servidor_mcp/request_log.py) grava stderr por requisição, incluindo id, método, nome, traceparent, HTTP e resultado; registra apenas a presença do estado. [`agente.tracing`](agente/src/agente/tracing.py) preserva trace-id por Task e gera span-id por chamada MCP.

Adaptações verificadas nos SDKs fixados, mantendo seus transportes e serialização:

- MCP `ClientSession._build_capabilities` anuncia form e URL quando recebe callback e sobrescreve capabilities no `_meta`. `FormClient._build_session` restringe o anúncio a `elicitation.form`, antes de `adopt()`. O callback automático lança erro caso seja acionado; `allow_input_required=True` entrega a pausa ao agente.
- MCP `ClientSession.validate_tool_result` faz um `list_tools()` implícito sem o trace da chamada quando uma sessão nova recebe um resultado sem catálogo local. `MCPHost.connect` alimenta `_absorb_tool_listing` com o catálogo previamente descoberto, preservando a validação do resultado e fazendo somente o retry na continuação. Esses dois pontos internos estão cobertos pelo teste de integração que grava bodies e headers HTTP; exigem revisão ao atualizar o SDK.
- MCP 2.3.0 cria um `JSONRPCDispatcher` por conexão (`Client._build_session`); seu construtor inicia `_next_id = 0` e `_allocate_id` incrementa esse contador. Assim, sessões curtas repetiam ids nos retries consecutivos e entre Tasks. Os construtores não oferecem gerador de ids; `CallOptions.request_id` existe no dispatcher, mas não é exposto por `ClientSession.call_tool`, `read_resource` ou `list_tools`. Em `FormClient._build_session`, após conferir o tipo do dispatcher e antes de entrar na sessão, adaptamos somente `_allocate_id` para consumir o mesmo `itertools.count(1)` pertencente ao `MCPHost`. Toda requisição desse host recebe um inteiro novo, inclusive em sessões concorrentes; o contador dura até o host ser recriado. O SDK mantém correlação, transporte HTTP, MRTR, capabilities, headers, tracing e serialização. Esse terceiro ponto interno também exige revisão ao atualizar o SDK.
- A2A `DefaultRequestHandler` é alias para `DefaultRequestHandlerV2`. Em `_setup_active_task`, ele consulta a Task mas passa `task=None` e o `contextId` recebido ao builder. `RequestContext._check_or_generate_context_id` gera outro UUID quando a continuação contém somente `taskId`, causando `Context in event doesn't match TaskManager`. [`StableTaskContextBuilder.build`](agente/src/agente/app.py) recupera o contexto salvo pelo task store quando ausente. O SDK continua validando contextos explícitos divergentes. O teste cobre três escolhas inválidas, `GetTask` e escolha válida, todas sem `contextId`, com identidade estável e sem chamadas MCP nas escolhas inválidas.
- [`V1ContextBuilder.build`](agente/src/agente/app.py) assume A2A 1.0 quando o header de versão é omitido pelo cliente do desafio. `HistoryTaskStore.save` inclui a mensagem corrente no histórico sem duplicação. O handler oficial recusa mensagens para Tasks terminais. Os erros de protocolo MCP e a leitura de recursos já cumprem o contrato no SDK; não foi necessário substituir ou mapear o protocolo manualmente.

O teste `test_mcp_restart_tamper_capability` inicia um MCP real em porta livre, obtém a pausa do wire 03, encerra e reinicia com a mesma chave, conclui o retry, rejeita token adulterado e confirma `-32021`/HTTP 400 sem capability. Confere também stderr, trace e ausência do token nos logs, encerrando o processo no `finally` da fixture.

O teste `test_repeated_retry_and_independent_tasks_have_unique_ids` usa MCP real e proxy HTTP: pausa duas Tasks de garagem 14–15, um chamador externo reserva fusca, a primeira Task escolhe fusca e pausa novamente oferecendo somente mirante, depois escolhe mirante e conclui. As três chamadas dessa Task têm ids distintos; todas as nove requisições do host, incluindo a outra Task e sua recusa, têm ids únicos. O teste confere identidade e contexto estáveis, trace preservado com spans novos, estado opaco de cada rodada reenviado sem alteração, ausência dos tokens nas respostas A2A e stderr, capabilities e headers. O teste de pausas concorrentes também verifica unicidade de todos os ids. As fixtures encerram MCP e proxy em `finally`.

## Saída do validador

Saída completa de `MCP_PORT=17301 AGENT_PORT=17300 bash scripts/validar-local.sh` (exit 0; stdout do validador salvo em `.runtime-logs/validator.log`). O harness confirmou o encerramento dos processos nas duas portas. O trace abaixo foi encontrado no stderr MCP, com descoberta antes das chamadas do agente e retry marcado `requestState="yes"`.

```text
trace-id desta execucao: 9061ed86305107d4b9567b02dabbf406
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
