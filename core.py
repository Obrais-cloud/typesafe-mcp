#!/usr/bin/env python3
"""
typesafe-mcp — MCP server exposing TypeSafe (Jev / System One) to the fleet.

Tools:
  judge      — natural language -> compiled TypeSafe request (via fleet LLM) -> executed answers
  rerank     — order a list of items by a natural-language criterion (comparable Score per item)
  systemone  — raw passthrough: you supply {state, questions}, it executes (no LLM)

Env:
  TYPESAFE_API_KEY   (required)
  TS_MCP_LLM_CHAIN   (optional JSON [{label,url,model,wake?}]) overrides the fleet chain
  TS_MCP_OLLAWAKE    (optional) default http://100.70.244.85:11436
(core logic — no MCP dependency)
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

TS_URL = os.environ.get("TS_MCP_TS_URL", "https://api.typesafe.ai/v1/systemone")
TS_MODEL_DEFAULT = "jev-latest"
OLLAWAKE = os.environ.get("TS_MCP_OLLAWAKE", "http://100.70.244.85:11436")
VALID_TYPES = {"noul", "choice", "score"}

DEFAULT_CHAIN = [
    {"label": "corsair",     "url": "http://100.94.117.48:11434/api/chat", "model": "qwen3.8:27b",     "wake": "corsair"},
    {"label": "corsair-alt", "url": "http://100.94.117.48:11434/api/chat", "model": "qwen3.6:35b-a3b"},
    {"label": "mac-studio",  "url": "http://100.68.94.14:11434/api/chat",  "model": "ornith-1.5:35b"},
    {"label": "alien18",     "url": "http://100.87.2.47:11434/api/chat",   "model": "qwen3:14b"},
]

COMPILER_SYSTEM = """\
You compile a user's natural-language request into a TypeSafe "System One" request.
TypeSafe does NOT generate text: it answers small typed judgments over a `state`.

Output ONLY a JSON object with exactly two keys:
  "state":     the content to evaluate (verbatim if embedded), or null if none is given.
  "questions": an object mapping a short snake_case id -> {type, instructions, criteria}.

type is one of:
  "noul"   yes/no condition. criteria optional or {"true":"...","false":"..."}.
  "choice" pick one of an unordered set. criteria = {"label": null, ...}.
  "score"  position on an ORDERED scale. criteria = ARRAY of ordered level descriptions,
           lowest first, each concrete and independently understandable (not bare words).

One narrow judgment per question. Do not invent judgments not asked for.
Return ONLY the JSON object, no prose, no markdown fences.
"""


def _key() -> str:
    k = os.environ.get("TYPESAFE_API_KEY")
    if not k:
        # Fallback: a .env next to this file or in ~/typesafe-mcp (key never in gateway config).
        for path in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                     os.path.expanduser("~/typesafe-mcp/.env")):
            try:
                with open(path) as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith("TYPESAFE_API_KEY="):
                            k = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            except OSError:
                continue
            if k:
                break
    if not k:
        raise RuntimeError("TYPESAFE_API_KEY not set (env or ~/typesafe-mcp/.env)")
    return k


def _chain() -> list:
    env = os.environ.get("TS_MCP_LLM_CHAIN")
    return json.loads(env) if env else DEFAULT_CHAIN


# ---- JSON repair / extraction ----------------------------------------------
def _balance_repair(s: str) -> str:
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"': in_str = True
        elif ch in "{[": stack.append(ch)
        elif ch in "}]":
            if stack: stack.pop()
    if in_str:
        return s
    return s + "".join("}" if c == "{" else "]" for c in reversed(stack))


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    i = text.find("{")
    if i > 0:
        text = text[i:]
    for cand in (text, _balance_repair(text)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError("LLM did not return valid JSON")


def _validate_questions(q) -> dict:
    if not isinstance(q, dict) or not q:
        raise ValueError("no 'questions' produced")
    out = {}
    for qid, spec in q.items():
        if not isinstance(spec, dict) or spec.get("type") not in VALID_TYPES:
            raise ValueError(f"question '{qid}' invalid")
        if not spec.get("instructions"):
            raise ValueError(f"question '{qid}' missing instructions")
        crit = spec.get("criteria")
        t = spec["type"]
        if t == "score" and not isinstance(crit, list):
            raise ValueError(f"question '{qid}' (score) needs an ordered list")
        if t == "choice" and not isinstance(crit, dict):
            raise ValueError(f"question '{qid}' (choice) needs an options object")
        item = {"type": t, "instructions": spec["instructions"]}
        if crit is not None:
            item["criteria"] = crit
        out[str(qid)] = item
    return out


# ---- fleet LLM (compile) with fallback -------------------------------------
async def _reachable(client: httpx.AsyncClient, url: str) -> bool:
    base = url.rsplit("/api/", 1)[0]
    try:
        r = await client.get(base + "/api/tags", timeout=4)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def _wake(client: httpx.AsyncClient, host: str) -> None:
    try:
        await client.post(f"{OLLAWAKE}/ensure/{host}", timeout=10)
    except httpx.HTTPError:
        pass


async def _llm_json(client: httpx.AsyncClient, messages: list) -> dict:
    """Try each fleet host until one returns compilable JSON. Returns {state, questions}."""
    errors, waked, i, chain = [], False, 0, _chain()
    while i < len(chain):
        c = chain[i]
        try:
            if not await _reachable(client, c["url"]):
                raise httpx.ConnectError("unreachable")
            payload = {"model": c["model"], "think": False, "stream": False, "format": "json",
                       "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 2048},
                       "messages": messages}
            r = await client.post(c["url"], json=payload, timeout=90)
            r.raise_for_status()
            return _extract_json(r.json().get("message", {}).get("content", ""))
        except (httpx.HTTPError, ValueError) as e:
            errors.append(f"{c.get('label', c['url'])}: {type(e).__name__}")
            if c.get("wake") and not waked and isinstance(e, httpx.HTTPError):
                waked = True
                await _wake(client, c["wake"])
                continue
            i += 1
    raise RuntimeError("no fleet host compiled the request: " + "; ".join(errors))


async def _ts_execute(client: httpx.AsyncClient, request: dict) -> dict:
    r = await client.post(TS_URL, json=request,
                          headers={"Authorization": f"Bearer {_key()}"}, timeout=60)
    r.raise_for_status()
    return r.json()


# ---- core operations (also unit-testable without MCP) ----------------------
async def do_judge(text: str, content: str | None = None, ts_model: str = TS_MODEL_DEFAULT) -> dict:
    user = text if content is None else f"{text}\n\n<content to evaluate>\n{content}"
    messages = [{"role": "system", "content": COMPILER_SYSTEM},
                {"role": "user", "content": user}]
    async with httpx.AsyncClient() as client:
        compiled = await _llm_json(client, messages)
        state = content if content is not None else compiled.get("state")
        questions = _validate_questions(compiled.get("questions"))
        if state is None:
            raise RuntimeError("no content to evaluate: pass it in the text or in `content`")
        request = {"model": ts_model, "state": state, "questions": questions}
        resp = await _ts_execute(client, request)
    return {"request": request, "answers": resp.get("answers", {}), "usage": resp.get("usage", {})}


async def do_systemone(state, questions: dict, ts_model: str = TS_MODEL_DEFAULT) -> dict:
    request = {"model": ts_model, "state": state, "questions": _validate_questions(questions)}
    async with httpx.AsyncClient() as client:
        resp = await _ts_execute(client, request)
    return {"model": resp.get("model"), "answers": resp.get("answers", {}), "usage": resp.get("usage", {})}


async def do_rerank(items: list, criterion: str, ts_model: str = TS_MODEL_DEFAULT,
                    levels: list | None = None, instructions: str | None = None) -> dict:
    """items: list of {id, text} (or strings). criterion: NL scale, e.g. 'urgency'.

    If `levels` (+ `instructions`) are supplied, the LLM compile step is skipped and items
    are scored directly against that fixed Score (fast, no fleet LLM). Otherwise the ordered
    levels are compiled once via the fleet LLM. The result always echoes levels+instructions
    so callers can cache them.
    """
    norm_items = [{"id": str(it.get("id", n)), "text": it["text"]} if isinstance(it, dict)
                  else {"id": str(n), "text": str(it)} for n, it in enumerate(items)]
    async with httpx.AsyncClient() as client:
        if levels and instructions:
            question = {"type": "score", "instructions": instructions, "criteria": list(levels)}
        else:
            prompt = (f"Build ordered levels for scoring items by this criterion: {criterion}. "
                      "Return ONLY JSON {\"questions\": {\"q\": {\"type\": \"score\", \"instructions\": \"...\", "
                      "\"criteria\": [\"lowest ...\", ..., \"highest ...\"]}}}. Concrete, independent levels.")
            compiled = await _llm_json(client, [{"role": "system", "content": COMPILER_SYSTEM},
                                                {"role": "user", "content": prompt}])
            questions = _validate_questions(compiled.get("questions"))
            question = questions[next(iter(questions))]
        levels = question["criteria"]
        instructions = question["instructions"]
        maxlvl = len(levels) - 1

        async def score_one(it):
            req = {"model": ts_model, "state": it["text"], "questions": {"q": question}}
            try:
                resp = await _ts_execute(client, req)
                a = resp["answers"]["q"]
                return {"id": it["id"], "score": round(a["score"], 3),
                        "norm": round(a["score"] / maxlvl, 3), "confidence": round(a["confidence"], 3),
                        "tokens": resp.get("usage", {}).get("input_tokens", 0)}
            except httpx.HTTPError as e:
                return {"id": it["id"], "error": str(e)}

        results = await asyncio.gather(*[score_one(it) for it in norm_items])
    ok = [r for r in results if "error" not in r]
    ok.sort(key=lambda r: r["score"], reverse=True)
    errs = [r for r in results if "error" in r]
    return {"criterion": criterion, "instructions": instructions, "levels": levels,
            "ranked": ok + errs, "total_tokens": sum(r.get("tokens", 0) for r in ok)}


