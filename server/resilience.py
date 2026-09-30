"""
Handles the "what happens when a tool fails" question directly:

  - Timeout        — a hung tool can't hang the whole agent turn forever
  - Retry w/ backoff — transient failures (a flaky network call, a lock
                       contention) get a couple of chances before giving up
  - Circuit breaker  — if a tool keeps failing, stop hammering it and fail
                       fast for a cooldown period instead — protects
                       whatever's on the other end of that tool, and gives
                       the agent a clear, immediate "this tool is down"
                       signal instead of a slow timeout every single call
"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

_executor = ThreadPoolExecutor(max_workers=8)

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 20


class CircuitOpenError(Exception):
    pass


class _CircuitState:
    def __init__(self):
        self.failure_count = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        return time.time() < self.open_until

    def record_success(self):
        self.failure_count = 0
        self.open_until = 0.0

    def record_failure(self):
        self.failure_count += 1
        if self.failure_count >= FAILURE_THRESHOLD:
            self.open_until = time.time() + COOLDOWN_SECONDS


_circuits: dict[str, _CircuitState] = {}


def _circuit_for(tool_name: str) -> _CircuitState:
    if tool_name not in _circuits:
        _circuits[tool_name] = _CircuitState()
    return _circuits[tool_name]


def get_circuit_states() -> dict:
    """For the observability report — shows which tools are currently
    considered unhealthy."""
    return {
        name: {
            "open": c.is_open(),
            "failure_count": c.failure_count,
            "reopens_in_seconds": max(0, round(c.open_until - time.time())),
        }
        for name, c in _circuits.items()
    }


def resilient_call(tool_name: str, func, *args, timeout: float = 5.0, max_retries: int = 2, **kwargs):
    """Runs func(*args, **kwargs) with a timeout, retrying transient
    failures with exponential backoff, short-circuiting immediately if the
    tool's circuit breaker is currently open."""
    circuit = _circuit_for(tool_name)

    if circuit.is_open():
        raise CircuitOpenError(
            f"Tool '{tool_name}' is temporarily disabled after repeated failures "
            f"— retry in {round(circuit.open_until - time.time())}s."
        )

    last_error = None
    for attempt in range(max_retries + 1):
        future = _executor.submit(func, *args, **kwargs)
        try:
            result = future.result(timeout=timeout)
            circuit.record_success()
            return result
        except FutureTimeoutError:
            last_error = TimeoutError(f"'{tool_name}' timed out after {timeout}s")
        except Exception as e:
            last_error = e

        if attempt < max_retries:
            time.sleep(0.5 * (2 ** attempt))  # exponential backoff: 0.5s, 1s, ...

    circuit.record_failure()
    raise last_error
