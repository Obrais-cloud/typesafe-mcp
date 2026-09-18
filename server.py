#!/usr/bin/env python3
"""
typesafe-mcp — MCP server exposing TypeSafe (Jev / System One) to the fleet.

Tools: judge (NL -> executed answers), rerank (order a list by a criterion),
systemone (raw {state, questions} passthrough). Logic lives in core.py.

Env: TYPESAFE_API_KEY (required); TS_MCP_LLM_CHAIN, TS_MCP_OLLAWAKE (optional).
Run: python3 server.py   (stdio MCP)
"""
from __future__ import annotations

import core

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


if __name__ == "__main__":
    mcp.run()
