#!/usr/bin/env python3
"""
Usage ledger for Jev/TypeSafe calls made by this MCP server.

One row per API call, sent fire-and-forget with a 2s timeout so a slow or broken
ledger can never delay or fail a judgment. Records METADATA ONLY: never the
state, only a SHA-256 hash of it (state_hash).

No-ops silently when SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY are absent (from
the environment or a .env next to this file / in ~/typesafe-mcp), so this is
safe on any machine that has not been given the ledger credentials.

The jev_calls table is RLS-locked to service_role, so this uses the service-role
key. That key lives only in the mini's .env, never in gateway config.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

import httpx

LEDGER_TIMEOUT_S = 2.0


def _env(name: str) -> str | None:
    """os.environ, then a .env next to this file or in ~/typesafe-mcp (like _key)."""
    v = os.environ.get(name)
    if v:
        return v
    for path in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                 os.path.expanduser("~/typesafe-mcp/.env")):
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(f"{name}="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def hash_state(state) -> str:
    """SHA-256 hex of a state. Objects are stringified first. Never reversible."""
    s = state if isinstance(state, str) else json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def _send(url: str, key: str, row: dict) -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                url.rstrip("/") + "/rest/v1/jev_calls",
                json=row,
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=LEDGER_TIMEOUT_S,
            )
    except Exception:
        # A broken ledger must never surface to the caller.
        pass


def _dispatch(url: str, key: str, row: dict) -> None:
    """Schedule the send without awaiting it. Never blocks the decision path."""
    coro = _send(url, key, row)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(coro)
    else:
        # No running loop (sync context, e.g. a test): run it to completion but
        # never let it raise. Still bounded by the 2s client timeout.
        try:
            asyncio.run(coro)
        except Exception:
            pass


def record_jev_call(*, caller: str, pack: str | None = None, model: str | None = None,
                    n_questions: int | None = None, input_tokens: int | None = None,
                    latency_ms: int | None = None, decision: str | None = None,
                    decision_mass: float | None = None, fallback: bool = False,
                    error: str | None = None, human_override: str | None = None,
                    state_hash: str | None = None) -> None:
    """Insert one ledger row, fire-and-forget. Never raises, never blocks."""
    try:
        url = _env("SUPABASE_URL")
        # jev_calls accepts the anon (publishable) role for this append-only
        # metadata table, so the service-role key is optional: use it if present,
        # else the anon key. Either lets this machine record without a secret key.
        key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
        if not url or not key:
            return  # ledger not configured on this machine; decision path unaffected
        row = {
            "caller": caller, "pack": pack, "model": model, "n_questions": n_questions,
            "input_tokens": input_tokens, "latency_ms": latency_ms, "decision": decision,
            "decision_mass": decision_mass, "fallback": fallback, "error": error,
            "human_override": human_override, "state_hash": state_hash,
        }
        row = {k: v for k, v in row.items() if v is not None}
        row.setdefault("fallback", fallback)
        _dispatch(url, key, row)
    except Exception:
        # Guarding the guard: nothing about the ledger may break a judgment.
        pass
