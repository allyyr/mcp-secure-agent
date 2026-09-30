"""
Observability, built directly on top of the audit log rather than a
separate tracing system — every tool call was already being logged for
security/audit purposes, so the same table doubles as the source of truth
for "is this thing healthy." That overlap is deliberate: audit and
observability data often come from the same event stream in real systems.

Usage:
    python observability_report.py
"""

import sqlite3
import statistics
from pathlib import Path

AUDIT_DB_PATH = Path(__file__).parent.parent / "server" / "audit.db"


def report():
    if not AUDIT_DB_PATH.exists():
        print("No audit log yet — run the server and make some tool calls first.")
        return

    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY ts").fetchall()]
    conn.close()

    if not rows:
        print("Audit log is empty.")
        return

    print(f"Total calls logged: {len(rows)}\n")

    by_tool: dict[str, list[dict]] = {}
    for r in rows:
        by_tool.setdefault(r["tool"], []).append(r)

    print(f"{'Tool':<20}{'Calls':<8}{'Success %':<12}{'Avg ms':<10}{'p95 ms':<10}{'Denied':<8}{'Errors':<8}{'Breaker trips':<14}")
    print("-" * 90)

    for tool, calls in by_tool.items():
        total = len(calls)
        successes = sum(1 for c in calls if c["status"] == "success")
        denied = sum(1 for c in calls if c["status"] == "denied")
        errors = sum(1 for c in calls if c["status"] == "error")
        breaker_trips = sum(1 for c in calls if c["status"] == "circuit_open")

        latencies = [c["latency_ms"] for c in calls if c["latency_ms"] is not None and c["status"] == "success"]
        avg_ms = round(statistics.mean(latencies), 1) if latencies else 0
        p95_ms = round(statistics.quantiles(latencies, n=20)[18], 1) if len(latencies) >= 5 else round(max(latencies, default=0), 1)
        success_pct = round(100 * successes / total, 1)

        print(f"{tool:<20}{total:<8}{success_pct:<12}{avg_ms:<10}{p95_ms:<10}{denied:<8}{errors:<8}{breaker_trips:<14}")

    print("\nMost recent 10 calls:")
    for r in rows[-10:]:
        error_note = f" — {r['error']}" if r["error"] else ""
        print(f"  [{r['status']:<12}] {r['tool']:<18} role={r['role']:<8} {r['latency_ms'] or 0:.0f}ms{error_note}")


if __name__ == "__main__":
    report()
