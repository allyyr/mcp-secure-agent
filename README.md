# Secure MCP Agent

A real, working answer to "how will your AI agent interact with real-world
systems, safely" — a spec-compliant MCP server (built on Anthropic's
official Python SDK) with authorization, resilience, and audit logging
wired through every tool call, plus a local LLM agent client that actually
uses it. Free, fully local, no API keys. 

Every piece here maps directly to a real interview question about MCP.
That mapping is the point of this README — read it as answers, not just
docs.
  
## "What is MCP, and why does it matter?"

MCP (Model Context Protocol) is a standard way for an LLM-based agent to
discover and call tools/resources exposed by a server — instead of every
integration being a bespoke, hand-wired function-calling setup, an MCP
server describes its tools once, and *any* MCP-compatible client (Claude
Desktop, Claude Code, or a custom agent like the one in `client/`) can use
them without integration-specific code.
 
`server/mcp_server.py` is a real MCP server — connect it to Claude Desktop
today (see "Connecting to Claude Desktop" below) and it works exactly the
same way it does with the custom client in this repo. That interchangeability
is the actual value MCP provides over a one-off API integration..

## "MCP vs a traditional API — when would you use which?"

A traditional API is called by code that already knows exactly which
endpoint to hit and how to parse the response — the integration is written
once, by a human, for a known shape of request.

MCP is for when the **caller is an LLM deciding at runtime** which tool it
needs and with what arguments — the server describes its own tools
(name, description, input schema) so the model can reason about which one
fits a natural-language request it wasn't specifically coded to handle. See
`client/agent_client.py`'s `decide_tool_call()` — it doesn't know in advance
whether the user wants `query_database` or `read_file`; it asks the LLM to
decide from the tool descriptions at runtime.

Use a plain API when the call pattern is fixed and known ahead of time.
Use MCP when an LLM needs to dynamically choose from a set of capabilities.

## "Design an agent that securely interacts with databases, APIs, and files."

That's exactly what this repo is. Three tools of deliberately increasing
risk:

| Tool | Risk | Access |
|---|---|---|
| `query_database` | read-only | reader, admin |
| `read_file` | read-only, sandboxed | reader, admin |
| `send_notification` | write/side-effecting | admin only |

The pattern worth noticing: **read-only tools are broadly available,
write/action tools are gated behind a stricter role.** This mirrors how
real enterprise systems scope agent permissions — an agent answering
questions shouldn't need the same privileges as one sending emails on your
behalf.

## "How would you secure MCP?" — `server/security.py`

- **Authorization**: `ROLE_PERMISSIONS` maps role → allowed tool names;
  `check_permission()` is called before any tool logic runs, not after.
- **Identity model**: this server runs over stdio (one process per client
  session — how Claude Desktop and most MCP hosts actually work), so role
  comes from an environment variable set at launch, not a per-request
  token. A multi-tenant HTTP/SSE deployment would move this to a
  per-request auth header instead — worth saying explicitly in an
  interview, since it shows you understand the transport affects the auth
  model, not just "add an API key" as a generic answer.
- **Redaction**: `redact()` masks SSNs and sensitive-looking keys
  (password/secret/token/ssn/api_key) before anything is written to the
  audit log — demonstrated with a deliberately sensitive `ssn` column in
  the demo database.
- **Auditability**: every call is logged — success, denial, or error —
  via `log_call()`, regardless of where in the pipeline it failed.
- **Input validation**: `query_database` restricts to SELECT-only, a table
  allowlist, and a forbidden-keyword check; `read_file` resolves the
  requested path and rejects anything outside the sandbox directory,
  defeating `../` traversal tricks. Both are demonstrated failing safely in
  the test output below.

## "How do you handle MCP failures?" — `server/resilience.py`

Three layers, applied to every tool call via `resilient_call()`:

1. **Timeout** — a hung tool call doesn't hang forever (`ThreadPoolExecutor`
   + `future.result(timeout=...)`).
2. **Retry with exponential backoff** — transient failures get up to 2
   retries (0.5s, 1s backoff) before giving up. `send_notification` is
   deliberately flaky (~30% failure rate) specifically to demonstrate this
   under realistic conditions rather than only in theory.
3. **Circuit breaker** — after 3 consecutive failures, a tool is marked
   "open" for a 20s cooldown, failing fast instead of continuing to hammer
   a struggling dependency. Status is visible per-tool via
   `resilience.get_circuit_states()`.

All of this is proven working, not just asserted — see the verification
output below.

## The full chain: LLM → Tool Calling → MCP → Agent → Observability → Security

`client/agent_client.py` is the piece that ties it together:

1. **LLM** (local, via Ollama — free) decides whether a tool is needed and
   which one, given the tool schemas MCP exposes.
2. **Tool calling** happens through a **structured JSON decision**, with a
   repair-retry if the model's output doesn't parse — the same "structured
   output reliability" pattern any production LLM feature needs.
3. **MCP** carries that decision to the real server via a spec-compliant
   `ClientSession` over stdio.
4. **Security** (role-based permission, sandboxing) and **resilience**
   (timeout/retry/breaker) run transparently inside the server — the agent
   doesn't need to know any of that exists.
5. **Observability**: `observability/observability_report.py` turns the
   same audit log into call volume, success rate, and latency percentiles
   per tool — the same event stream serves both audit and observability
   purposes, which is realistic, not two separate systems bolted together.

## Setup

### Server

```bash
cd server
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Test it standalone (reads/writes via stdio, so it'll look like it's
# hanging — that's normal, it's waiting for an MCP client to talk to it)
MCP_CLIENT_ROLE=reader python mcp_server.py
```

### Client (needs Ollama for the local LLM)

```bash
# Install Ollama from https://ollama.com, then:
ollama pull llama3.1

cd client
pip install -r requirements.txt

python agent_client.py "How many orders has Amina Kader placed?"
python agent_client.py --role admin "Send a notification to #sales saying the demo is ready"
python agent_client.py "Read the welcome file"
python agent_client.py "Send a notification saying hi"   # --role defaults to reader — watch this get denied
```

### Observability report

```bash
cd observability
python observability_report.py
```

### Connecting to Claude Desktop (the realistic MCP host demo)

Add to Claude Desktop's config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "secure-agent-demo": {
      "command": "python",
      "args": ["/absolute/path/to/server/mcp_server.py"],
      "env": { "MCP_CLIENT_ROLE": "admin" }
    }
  }
}
```

Restart Claude Desktop, and you can ask Claude itself to query the demo
database or send a notification — the exact same server, same security and
resilience logic, now driven by a production MCP host instead of the
custom client.

## Verification (this actually works, confirmed during build)

```
Query result: [{'name': 'Amina Kader', 'email': 'amina@example.com'}]
Correctly denied: Role 'reader' is not permitted to call 'send_notification'.
Correctly blocked path traversal: Path escapes the sandboxed directory — request denied.
File read OK, content: This is a sandboxed file. The read_file...
Correctly blocked non-SELECT: Only SELECT statements are allowed.
```

And the observability report after a mixed batch of calls:

```
Tool                Calls   Success %   Avg ms    p95 ms    Denied  Errors  Breaker trips
--------------------------------------------------------------------------------------------
query_database      2       50.0        0.7       0.7       0       1       0
send_notification   9       77.8        500.4     1500.8    1       1       0
read_file           2       50.0        0.3       0.3       0       1       0
```

Notice the 1500.8ms figures for `send_notification` — those are calls that
failed once or twice internally, slept through exponential backoff, then
succeeded on retry. That's the resilience layer's actual behavior showing
up in real latency numbers, not just a comment claiming it works.

## Known limitations / honest next steps

- **Demo-level SQL guard**: a regex denylist + table allowlist, not a
  production SQL sandbox. Real deployments should use a read-only DB
  connection role or a query builder instead of trusting string inspection.
- **Single-process circuit breaker state**: lives in memory in the server
  process, not visible to the separate observability script directly (the
  report infers breaker trips from the audit log's `circuit_open` status
  instead). A live "get circuit status" MCP tool/resource would be the
  natural next step for real-time introspection.
- **stdio transport only**: this demonstrates the single-client-per-process
  model most MCP hosts use today. An HTTP/SSE version would need per-request
  auth instead of the environment-variable role used here — a good
  "what would change" answer if asked in an interview.
