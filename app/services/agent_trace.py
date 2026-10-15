"""Bounded, payload-free request traces. Process-local; never a business receipt."""
from collections import OrderedDict
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
from time import monotonic, perf_counter
from uuid import uuid4

from fastapi import HTTPException

TTL_SECONDS = 86400
MAX_TRACES = 1000
MAX_PER_USER = 50
MAX_SPANS = 128
_current = ContextVar("agent_trace", default=None)
_parent = ContextVar("agent_trace_parent", default=None)
_traces = OrderedDict()


def annotate_stage(*, usage=None, error_type=None):
    trace = _current.get()
    if trace is None or trace.get("closed"):
        return
    span = next((item for item in trace["spans"] if item["id"] == _parent.get()), None)
    if span is None:
        return
    if error_type in {"ReadTimeout", "ConnectTimeout", "ConnectError", "HTTPStatusError",
                      "KeyError", "IndexError", "TypeError", "ValueError", "JSONDecodeError"}:
        span["error_type"] = error_type
    if isinstance(usage, dict):
        span["usage"] = {key: value for key, value in usage.items()
                         if key in {"input_tokens", "output_tokens", "total_tokens",
                                    "prompt_tokens", "completion_tokens", "cached_tokens"}
                         and type(value) is int and value >= 0}


def _prune():
    now = monotonic()
    for key, (expires, _) in list(_traces.items()):
        if expires <= now:
            del _traces[key]


def _save(trace):
    _prune()
    owner_keys = [key for key, (_, value) in _traces.items()
                  if value["user_id"] == trace["user_id"]]
    for key in owner_keys[:max(0, len(owner_keys) - MAX_PER_USER + 1)]:
        del _traces[key]
    _traces[trace["trace_id"]] = (monotonic() + TTL_SECONDS, deepcopy(trace))
    while len(_traces) > MAX_TRACES:
        _traces.popitem(last=False)


def read_traces(user_id, trip_id, trace_id=None):
    _prune()
    values = [deepcopy(value) for _, value in reversed(list(_traces.values()))
              if value["user_id"] == user_id and value["trip_id"] == trip_id
              and (trace_id is None or value["trace_id"] == trace_id)]
    return values


def traced_request(operation):
    """Wrap resolved endpoints, retaining their FastAPI signature and transactions."""
    def decorate(fn):
        sig = signature(fn)

        @wraps(fn)
        async def wrapped(*args, **kwargs):
            bound = sig.bind(*args, **kwargs).arguments
            trace = {"trace_id": uuid4().hex, "user_id": bound["user"].id,
                     "trip_id": bound["trip_id"], "operation": operation,
                     "started_at": datetime.now(timezone.utc).isoformat(),
                     "status": "ok", "spans": [], "dropped_spans": 0}
            if operation == "execute":
                trace["proposal_id"] = str(bound["body"].proposal_id) if "body" in bound else None
            token = _current.set(trace)
            parent_token = _parent.set(None)
            started = perf_counter()
            try:
                result = await fn(*args, **kwargs)
                # Only explicitly allowed summaries; no actions, reply or proposal contents.
                if isinstance(result, dict):
                    if isinstance(result.get("proposal_id"), str):
                        trace["proposal_id"] = result["proposal_id"]
                    if operation == "execute":
                        trace["replayed"] = bool(result.get("replayed", False))
                    if operation == "chat":
                        trace["llm_used"] = bool(result.get("llm_used", False))
                    return {**result, "trace_id": trace["trace_id"]}
                return result
            except BaseException as exc:
                trace["status"] = "cancelled" if type(exc).__name__ == "CancelledError" else "error"
                trace["error_type"] = type(exc).__name__
                if isinstance(exc, HTTPException):
                    trace["http_status"] = exc.status_code
                    exc.headers = {**(exc.headers or {}), "X-Agent-Trace-Id": trace["trace_id"]}
                raise
            finally:
                trace["duration_ms"] = round((perf_counter() - started) * 1000, 3)
                trace["closed"] = True
                try:
                    _save(trace)
                except Exception:
                    pass  # Telemetry must never replace a business result/error.
                _parent.reset(parent_token)
                _current.reset(token)
        return wrapped
    return decorate


def traced_stage(name, outcome=None):
    """Fixed stage names, nested IDs, timing; never serialize arguments/results."""
    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            trace = _current.get()
            if trace is None or trace.get("closed"):
                return await fn(*args, **kwargs)
            if len(trace["spans"]) >= MAX_SPANS:
                trace["dropped_spans"] += 1
                return await fn(*args, **kwargs)
            span = {"id": len(trace["spans"]) + 1, "parent_id": _parent.get(),
                    "stage": name, "status": "ok"}
            trace["spans"].append(span)
            token = _parent.set(span["id"])
            started = perf_counter()
            try:
                result = await fn(*args, **kwargs)
                if outcome == "nullable" and result is None:
                    span["status"] = "unavailable"
                elif outcome == "model" and isinstance(result, tuple):
                    span["status"] = "ok" if result[0] is not None else "unavailable"
                elif outcome == "parse" and isinstance(result, tuple):
                    span["status"] = "ok" if result[1] is None else "unavailable"
                elif outcome == "assistant" and isinstance(result, tuple):
                    span["status"] = "ok" if result[1] else "fallback"
                elif outcome == "route" and isinstance(result, dict) and result.get("estimated"):
                    span["status"] = "estimated"
                return result
            except BaseException as exc:
                span["status"] = "error"
                span["error_type"] = type(exc).__name__
                raise
            finally:
                span["duration_ms"] = round((perf_counter() - started) * 1000, 3)
                _parent.reset(token)
        return wrapped
    return decorate
