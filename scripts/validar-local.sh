#!/usr/bin/env bash
# Executa o validador em processos novos e encerra ambos ao sair.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export MCP_PORT="${MCP_PORT:-7301}"
export AGENT_PORT="${AGENT_PORT:-7300}"
export MCP_HOST=127.0.0.1 AGENT_HOST=127.0.0.1
export MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp"
export AGENT_PUBLIC_URL="http://127.0.0.1:${AGENT_PORT}"
if [[ -z "${REQUEST_STATE_SECRET:-}" ]]; then
  REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  export REQUEST_STATE_SECRET
fi
LOG_DIR="${LOG_DIR:-$ROOT/.runtime-logs}"
mkdir -p "$LOG_DIR"
MCP_PID='' AGENT_PID=''
cleanup() {
  local code=$?
  trap - EXIT INT TERM
  for pid in "$AGENT_PID" "$MCP_PID"; do
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
  done
  for pid in "$AGENT_PID" "$MCP_PID"; do
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
  done
  echo "Processos encerrados: sim (MCP=$MCP_PORT, agente=$AGENT_PORT)" >&2
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Falha antes de iniciar se uma porta já estiver ocupada.
python3 - <<'PY'
import os, socket
for name in ('MCP_PORT', 'AGENT_PORT'):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', int(os.environ[name])))
PY
uv run --locked --project servidor-mcp servidor-mcp > "$LOG_DIR/mcp.stdout.log" 2> "$LOG_DIR/mcp.stderr.log" &
MCP_PID=$!
uv run --locked --project agente agente > "$LOG_DIR/agent.stdout.log" 2> "$LOG_DIR/agent.stderr.log" &
AGENT_PID=$!
python3 - <<'PY'
import os, time, urllib.request, urllib.error
for name, path in [('MCP_PORT', '/mcp'), ('AGENT_PORT', '/.well-known/agent-card.json')]:
    url = f'http://127.0.0.1:{os.environ[name]}{path}'
    deadline = time.monotonic() + 30
    while True:
        try:
            urllib.request.urlopen(url, timeout=1).close()
            break
        except urllib.error.HTTPError:
            break
        except (urllib.error.URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise SystemExit(f'Serviço não iniciou: {name}; consulte os logs')
            time.sleep(0.2)
PY
python3 validador/validar.py --agente "http://127.0.0.1:$AGENT_PORT" --mcp "http://127.0.0.1:$MCP_PORT" | tee "$LOG_DIR/validator.log"
