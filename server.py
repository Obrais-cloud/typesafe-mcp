#!/usr/bin/env python3
"""
typesafe-mcp — MCP server exposing TypeSafe (Jev / System One) to the fleet.

Tools: judge (NL -> executed answers), rerank (order a list by a criterion),
systemone (raw {state, questions} passthrough). Logic lives in core.py.

Env: TYPESAFE_API_KEY (required); TS_MCP_LLM_CHAIN, TS_MCP_OLLAWAKE (optional).
Run: python3 server.py   (stdio MCP)
"""
from __future__ import annotations

import asyncio
import hmac
import os

import core
import jevkit
import jobfit
import ledger

# Compat: mcp v1 exposes FastMCP; v2 renamed it to MCPServer. Both have .tool()/.run().
try:
    from mcp.server.fastmcp import FastMCP as _MCP
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _MCP

mcp = _MCP("typesafe")


@mcp.tool()
async def judge(text: str, content: str | None = None,
                ts_model: str = core.TS_MODEL_DEFAULT) -> dict:
    """Judge content with TypeSafe from a natural-language request.

    Describe the judgments in plain language (optionally embedding the content, or pass it
    in `content`). A fleet LLM compiles it into a typed TypeSafe request (noul/choice/score)
    and it is executed. Returns the compiled request plus typed answers with probabilities.
    """
    return await core.do_judge(text, content, ts_model)


@mcp.tool()
async def rerank(items: list, criterion: str,
                 ts_model: str = core.TS_MODEL_DEFAULT) -> dict:
    """Rank items by a natural-language criterion using a comparable TypeSafe Score per item.

    items: list of {"id","text"} (or plain strings). criterion: the ordered dimension,
    e.g. "urgency". Returns items sorted desc by score, each with score/norm/confidence.
    """
    return await core.do_rerank(items, criterion, ts_model)


@mcp.tool()
async def systemone(state, questions: dict,
                    ts_model: str = core.TS_MODEL_DEFAULT) -> dict:
    """Execute a raw TypeSafe request you already have (no LLM compilation).

    state: text or structured content. questions: map id -> {type, instructions, criteria}.
    Use this when you already know the exact typed questions; use `judge` otherwise.
    """
    return await core.do_systemone(state, questions, ts_model)


# ── canonical shared judgments (jevkit): same definitions the fleet's CLIs use ──
@mcp.tool()
async def quality_judge(prompt: str, response: str, criteria: str | None = None) -> dict:
    """Grade one response against criteria with a calibrated Jev Score.

    Returns a normalized 0..1 `value` plus raw score, confidence and the full
    distribution. This is the SAME judge ollaeval/localeval/olladiff use, so
    scores are comparable across the fleet.
    """
    return await asyncio.to_thread(jevkit.quality_judge, prompt, response, criteria)


@mcp.tool()
async def compare(prompt: str, response_a: str, response_b: str,
                  criteria: str | None = None) -> dict:
    """Pick the better of two responses to the same prompt (Jev Choice).

    Returns winner ('a' | 'b' | 'tie'), confidence and the probability spread —
    the same pairwise judge ollarena/olladiff use.
    """
    return await asyncio.to_thread(jevkit.compare_pair, prompt, response_a, response_b, criteria)


@mcp.tool()
async def classify_push_failure(error_text: str) -> dict:
    """Classify a git push failure from its error text (Jev).

    Returns the typed cause + probabilities, whether a history rewrite is likely
    needed, a severity score, and code-owned suggested fixes.
    """
    return await asyncio.to_thread(jevkit.classify_push_failure, error_text)


@mcp.tool()
async def job_fit(posting: dict) -> dict:
    """Triage a job posting for the nightly employment routine (career-ops + TypeSafe).

    posting: {title, company?, location?, url?, body}. Runs the real job-fit thresholds
    by subprocess and returns {decision, action, fit, reasons[], cost_usd, title}, where
    decision is auto_apply | joint_eval | drop, or "manual" (with a reason) if job-fit
    could not run. Every failure hands the posting to a person, never to auto-apply.
    """
    return await jobfit.run_job_fit(posting)


def _bearer_guard(app, token: str):
    """ASGI wrapper: require `Authorization: Bearer <token>` on every HTTP request.
    Lifespan and other scopes pass through untouched so the app starts normally."""
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"")
            if not hmac.compare_digest(got, expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)

    return guarded


def _run_http(port: int) -> None:
    """Serve the SAME MCP over streamable-HTTP, for clients that cannot do stdio
    over SSH (e.g. Windows OpenSSH). Bind to a private host (the mini's Tailscale
    IP) and require a bearer token when TS_MCP_HTTP_TOKEN is set. The TypeSafe key
    never leaves the mini: this only changes the transport, not where it runs."""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    host = ledger._env("TS_MCP_HTTP_HOST") or "127.0.0.1"
    token = ledger._env("TS_MCP_HTTP_TOKEN")
    # The clients here are MCP CLIs, not browsers, so DNS-rebinding protection (a
    # browser-only attack) does not apply and would otherwise 421 any non-local
    # Host header. Access is gated by the bearer token and the private Tailscale
    # bind instead.
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(transport_security=sec, host=host)
    if token:
        app = _bearer_guard(app, token)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    # HTTP mode is opt-in via a real environment variable ONLY (set by the
    # dedicated launchd service). It is deliberately NOT read from .env, so the
    # many stdio spawns of this server (openclaw/hermes/mac-studio/…) stay stdio.
    _http_port = os.environ.get("TS_MCP_HTTP_PORT")
    if _http_port:
        _run_http(int(_http_port))
    else:
        mcp.run()
