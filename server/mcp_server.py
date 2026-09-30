"""
A real, spec-compliant MCP server (built on Anthropic's official Python SDK,
FastMCP) exposing three tools of deliberately increasing risk:

  query_database    (read-only, low risk)      -> reader + admin
  read_file         (read-only, sandboxed)      -> reader + admin
  send_notification (write/side-effecting)      -> admin only

Every tool call passes through the same three gates before doing real work:
  1. security.check_permission  — is this role allowed to call this tool?
  2. resilience.resilient_call  — timeout, retry, circuit breaker
  3. security.log_call          — audit trail, redacted, regardless of outcome

Run standalone for testing:
    python mcp_server.py

Connect from Claude Desktop by adding to its config (see README) — that's
the realistic "plug this into a real MCP host" demonstration, since that's
literally what MCP is for.
"""

import random
import re
import sqlite3
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import security
import resilience
from seed_db import seed, DB_PATH

SANDBOX_DIR = Path(__file__).parent / "sandbox_files"
NOTIFICATIONS_LOG = Path(__file__).parent / "notifications.log"

seed()
security.init_audit_db()
SANDBOX_DIR.mkdir(exist_ok=True)
if not any(SANDBOX_DIR.iterdir()):
    (SANDBOX_DIR / "welcome.txt").write_text(
        "This is a sandboxed file. The read_file tool can only ever see files in this folder.\n"
    )

mcp = FastMCP("secure-agent-demo")


def _run_guarded(tool_name: str, params: dict, work_fn, timeout: float = 5.0):
    """Shared plumbing for every tool: auth check -> resilient execution ->
    audit log. Any exception (permission, circuit-open, tool failure) is
    logged before being re-raised, so the audit trail is complete even for
    calls that never actually ran."""
    start = time.time()
    try:
        role = security.check_permission(tool_name)
    except security.PermissionDenied as e:
        security.log_call(tool_name, security.get_current_role(), params, "denied", 0, str(e))
        raise

    try:
        result = resilience.resilient_call(tool_name, work_fn, timeout=timeout)
        latency_ms = (time.time() - start) * 1000
        security.log_call(tool_name, role, params, "success", latency_ms, None)
        return result
    except resilience.CircuitOpenError as e:
        security.log_call(tool_name, role, params, "circuit_open", 0, str(e))
        raise
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        security.log_call(tool_name, role, params, "error", latency_ms, str(e))
        raise


# ---------- tool: query_database ----------

ALLOWED_TABLES = {"customers", "orders"}
FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|CREATE)\b", re.IGNORECASE
)


@mcp.tool()
def query_database(sql: str) -> str:
    """Run a read-only SQL query against the demo company database
    (tables: customers, orders). Only SELECT statements are permitted.

    Note for reviewers: this is a demo-level guard (regex denylist + table
    allowlist), not a production SQL sandbox. A real deployment should use
    a query builder or a read-only DB role at the connection level instead
    of trusting string inspection alone.
    """
    def work():
        stripped = sql.strip().rstrip(";")
        if not stripped.upper().startswith("SELECT"):
            raise ValueError("Only SELECT statements are allowed.")
        if FORBIDDEN_SQL.search(stripped):
            raise ValueError("Query contains a forbidden keyword.")
        if ";" in stripped:
            raise ValueError("Multiple statements are not allowed.")
        referenced_tables = set(re.findall(r"\bFROM\s+(\w+)|\bJOIN\s+(\w+)", stripped, re.IGNORECASE))
        referenced_tables = {t for pair in referenced_tables for t in pair if t}
        if not referenced_tables.issubset(ALLOWED_TABLES):
            raise ValueError(f"Query may only reference: {ALLOWED_TABLES}")

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(stripped).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    result = _run_guarded("query_database", {"sql": sql}, work)
    return str(result)


# ---------- tool: read_file ----------

@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file from the sandboxed files directory. Cannot access
    anything outside that directory, regardless of path tricks like '../'."""
    def work():
        requested = (SANDBOX_DIR / path).resolve()
        if not str(requested).startswith(str(SANDBOX_DIR.resolve())):
            raise PermissionError("Path escapes the sandboxed directory — request denied.")
        if not requested.exists():
            raise FileNotFoundError(f"No such file: {path}")
        return requested.read_text()

    return _run_guarded("read_file", {"path": path}, work)


# ---------- tool: send_notification (gated, write action, deliberately flaky) ----------

@mcp.tool()
def send_notification(message: str, channel: str = "general") -> str:
    """Send a notification to a channel. Admin-role only — this is a
    side-effecting action, unlike the two read-only tools above, so it sits
    behind a stricter permission than query_database or read_file.

    Deliberately fails ~30% of the time to demonstrate the retry/circuit
    breaker logic in resilience.py under realistic flaky-dependency
    conditions — remove the random failure in a real integration.
    """
    def work():
        if random.random() < 0.3:
            raise ConnectionError("Simulated transient failure contacting notification service.")
        with NOTIFICATIONS_LOG.open("a") as f:
            f.write(f"[{channel}] {message}\n")
        return f"Notification sent to #{channel}"

    return _run_guarded("send_notification", {"message": message, "channel": channel}, work, timeout=3.0)


if __name__ == "__main__":
    mcp.run()
