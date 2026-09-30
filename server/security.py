"""
Security layer for the MCP server. Three concerns, deliberately separated:

  1. Authorization  — which role can call which tool
  2. Redaction      — sensitive values never reach the audit log in plaintext
  3. Auditability   — every call is logged, success or failure, regardless
                      of whether authorization or the tool itself failed

Identity model: this server runs over stdio, meaning one process per client
session (this is how Claude Desktop and most MCP hosts spawn servers) — so
"who is calling" is the role the server was launched as, read from an
environment variable, not a per-request header. A multi-tenant HTTP/SSE
deployment would move this to a per-request auth token instead; noted in
the README as the real-world difference between the two transports.
"""

import json
import re
import sqlite3
import time
from pathlib import Path

AUDIT_DB_PATH = Path(__file__).parent / "audit.db"

ROLE_PERMISSIONS = {
    "reader": {"query_database", "read_file"},
    "admin": {"query_database", "read_file", "send_notification"},
}

SENSITIVE_KEY_PATTERN = re.compile(r"(password|secret|token|ssn|api_key)", re.IGNORECASE)
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class PermissionDenied(Exception):
    pass


def get_current_role() -> str:
    import os
    return os.environ.get("MCP_CLIENT_ROLE", "reader")


def check_permission(tool_name: str, role: str = None) -> str:
    """Raises PermissionDenied if role can't call tool_name. Returns the role
    used, so callers always have it on hand for audit logging."""
    role = role or get_current_role()
    allowed = ROLE_PERMISSIONS.get(role, set())
    if tool_name not in allowed:
        raise PermissionDenied(f"Role '{role}' is not permitted to call '{tool_name}'.")
    return role


def redact(value) -> str:
    """Redacts sensitive values before they're written to the audit log.
    Applied to both tool arguments and tool results — a query result
    containing an SSN column is just as sensitive as an SSN in the input."""
    text = json.dumps(value) if not isinstance(value, str) else value
    text = SSN_PATTERN.sub("[REDACTED-SSN]", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = {
                k: ("[REDACTED]" if SENSITIVE_KEY_PATTERN.search(k) else v)
                for k, v in parsed.items()
            }
            return json.dumps(parsed)
    except (json.JSONDecodeError, TypeError):
        pass
    return text


def init_audit_db():
    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            tool TEXT NOT NULL,
            role TEXT NOT NULL,
            params_redacted TEXT,
            status TEXT NOT NULL,
            latency_ms REAL,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def log_call(tool: str, role: str, params: dict, status: str, latency_ms: float, error: str = None):
    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.execute(
        "INSERT INTO calls (ts, tool, role, params_redacted, status, latency_ms, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), tool, role, redact(params), status, latency_ms, error),
    )
    conn.commit()
    conn.close()
