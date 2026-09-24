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
import time

import httpx

import ledger

TS_URL = os.environ.get("TS_MCP_TS_URL", "https://api.typesafe.ai/v1/systemone")
TS_MODEL_DEFAULT = (os.environ.get("TYPESAFE_MODEL") or "jev-1.13.0")  # fijado: los alias se mueven, los umbrales no
OLLAWAKE = os.environ.get("TS_MCP_OLLAWAKE", "http://100.70.244.85:11436")
VALID_TYPES = {"noul", "choice", "score"}
# v1 API limits, from docs.typesafe.ai/api and /models.
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
MAX_CHOICE_OPTIONS = 255

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

Design rules, from the TypeSafe agent skill and the live docs:
- Score levels must describe concrete SITUATIONS, each understandable on its own.
  The model never sees a level's number or its neighbours, so "worse than the one
  above", bare adjectives and bare numbers all carry no information. 2 to 10 levels.
- Keep one Score to ONE dimension. "punctual and skilled and experienced" is three
  questions; an item high on one and low on another cannot be placed at all.
- Give a Choice every option that could apply, and add an explicit no-match option
  ("other", "none of the above") whenever the list might not cover an input. The
  model cannot choose a value that was not sent.
- Use one Noul per label when several labels may apply at once. A Choice is
  relative and settles WHICH option wins; a Noul is absolute and can be low for
  every label.
- A Noul near 0.5 means yes and no are equally likely, NOT medium intensity. If
  the answer is a degree, use a Score with described levels instead.
- Atomic does not mean trivial. Splitting is for independently useful dimensions;
  do not split so far that the relationship being judged is destroyed. A bounded
  action selection or a contextual interpretation is one judgment.
- Never ask for something code computes exactly: arithmetic, counting, date
  comparison, sorting. Ask for the judgment and leave the maths to the caller.
- Put contrasts in structured criteria when two options keep getting confused:
  an object with what it covers, what it does NOT cover, and a few examples.

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
        # Limits from the v1 API contract. A compiled question that breaks one of
        # these comes back as a 422 from the service, which is a worse place to
        # find out than here: the fleet LLM happily emits 12 levels if asked.
        if t == "score":
            if not isinstance(crit, list):
                raise ValueError(f"question '{qid}' (score) needs an ordered list")
            if not (MIN_SCORE_LEVELS <= len(crit) <= MAX_SCORE_LEVELS):
                raise ValueError(
                    f"question '{qid}' (score) has {len(crit)} levels; "
                    f"the API accepts {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS}"
                )
            if any(not str(c).strip() for c in crit):
                raise ValueError(f"question '{qid}' (score) has an empty level")
        if t == "choice":
            if not isinstance(crit, dict):
                raise ValueError(f"question '{qid}' (choice) needs an options object")
            if not crit:
                raise ValueError(f"question '{qid}' (choice) has no options")
            if len(crit) > MAX_CHOICE_OPTIONS:
                raise ValueError(
                    f"question '{qid}' (choice) has {len(crit)} options; "
                    f"the API accepts at most {MAX_CHOICE_OPTIONS}"
                )
        if t == "noul" and crit is not None:
            if not isinstance(crit, dict) or set(crit) - {"true", "false"}:
                raise ValueError(f"question '{qid}' (noul) criteria must be {{true, false}}")
        item = {"type": t, "instructions": spec["instructions"]}
        if crit is not None:
            item["criteria"] = crit
        out[str(qid)] = item
    return out


# ---- batching --------------------------------------------------------------
# The context budget is 64k tokens for the whole request and 32k for the state
# plus the single longest question. Characters are a crude proxy for tokens, so
# this stays well under: roughly 4 chars per token, and half the 32k budget.
MAX_CHUNK_CHARS = 48_000
MAX_CHUNK_ITEMS = 120


def _chunk_items(items: list) -> list:
    """Split items into batches that fit one request. An item too large to share
    a request travels alone rather than being dropped or truncated."""
    chunks, current, size = [], [], 0
    for it in items:
        n = len(it["text"]) + 64  # the item's own text plus its question overhead
        if n >= MAX_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current, size = [], 0
            chunks.append([it])
            continue
        if current and (size + n > MAX_CHUNK_CHARS or len(current) >= MAX_CHUNK_ITEMS):
            chunks.append(current)
            current, size = [], 0
        current.append(it)
        size += n
    if current:
        chunks.append(current)
    return chunks


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


async def _ts_execute(client: httpx.AsyncClient, request: dict,
                      caller: str = "mcp.systemone", pack: str | None = None) -> dict:
    # Single point every judge/rerank/systemone call passes through, so the
    # usage ledger records here. Fire-and-forget; a ledger failure never breaks
    # the judgment (record_jev_call cannot raise, and the whole call is guarded).
    started = time.monotonic()
    state_hash = ledger.hash_state(request.get("state"))
    n_questions = len(request.get("questions") or {})
    try:
        r = await client.post(TS_URL, json=request,
                              headers={"Authorization": f"Bearer {_key()}"}, timeout=60)
        r.raise_for_status()
        resp = r.json()
    except Exception as e:
        try:
            ledger.record_jev_call(
                caller=caller, pack=pack, model=request.get("model"),
                n_questions=n_questions, latency_ms=int((time.monotonic() - started) * 1000),
                fallback=True, error=str(e)[:500], state_hash=state_hash)
        except Exception:
            pass
        raise
    try:
        ledger.record_jev_call(
            caller=caller, pack=pack, model=request.get("model"),
            n_questions=n_questions,
            input_tokens=(resp.get("usage") or {}).get("input_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
            fallback=False, state_hash=state_hash)
    except Exception:
        pass
    return resp


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
        resp = await _ts_execute(client, request, caller="mcp.judge")
    return {"request": request, "answers": resp.get("answers", {}), "usage": resp.get("usage", {})}


async def do_systemone(state, questions: dict, ts_model: str = TS_MODEL_DEFAULT) -> dict:
    request = {"model": ts_model, "state": state, "questions": _validate_questions(questions)}
    async with httpx.AsyncClient() as client:
        resp = await _ts_execute(client, request, caller="mcp.systemone")
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

        # One request per item costs the question tokens N times and pays N round
        # trips. Putting every item in a single `state` and asking one Score per
        # item is the pattern the rerank and semantic-find cookbooks use, and the
        # parallel-questions cookbook measures it at 12.2x cheaper and 10x faster
        # with no change in the answers. Questions are evaluated in isolation, so
        # each one still judges only the item its instructions name.
        chunks = _chunk_items(norm_items)

        async def score_chunk(chunk: list) -> list:
            # Items are keyed by a safe synthetic name, not by array position.
            #
            # Measured on 12 support tickets: with each item carrying a visible
            # `id` field AND the question addressing it positionally
            # (`items[4].text`), six of the twelve answers came back judging the
            # wrong item. Dropping the id, naming the id in the question, or
            # keying the object by id each scored twelve out of twelve. Mixing a
            # positional reference with a visible id label is what breaks it.
            #
            # Named keys also avoid making the model count to the Nth element,
            # which the docs list as unreliable and worse as N grows. The
            # caller's own ids may contain dots or spaces, so they are not used
            # as keys directly; they are mapped back after.
            keys = [f"i{n}" for n in range(len(chunk))]
            state = {"items": {k: it["text"] for k, it in zip(keys, chunk)}}
            questions = {
                k: {
                    "type": "score",
                    "instructions": f"Judging only `items.{k}`: {instructions}",
                    "criteria": list(levels),
                }
                for k in keys
            }
            try:
                resp = await _ts_execute(client, {"model": ts_model, "state": state,
                                                  "questions": questions}, caller="mcp.rerank")
            except httpx.HTTPError as e:
                return [{"id": it["id"], "error": str(e)} for it in chunk]

            used = resp.get("usage", {}).get("input_tokens", 0)
            # Attribute the shared state cost across the chunk so `total_tokens`
            # stays comparable with the unbatched numbers callers saw before.
            per_item = round(used / len(chunk), 1) if chunk else 0
            out = []
            for k, it in zip(keys, chunk):
                a = resp["answers"].get(k)
                if a is None:
                    out.append({"id": it["id"], "error": "no answer returned"})
                    continue
                out.append({"id": it["id"], "score": round(a["score"], 3),
                            "norm": round(a["score"] / maxlvl, 3),
                            "confidence": round(a["confidence"], 3),
                            "tokens": per_item})
            return out

        gathered = await asyncio.gather(*[score_chunk(c) for c in chunks])
        results = [r for chunk_result in gathered for r in chunk_result]
    ok = [r for r in results if "error" not in r]
    ok.sort(key=lambda r: r["score"], reverse=True)
    errs = [r for r in results if "error" in r]
    return {"criterion": criterion, "instructions": instructions, "levels": levels,
            "ranked": ok + errs, "total_tokens": round(sum(r.get("tokens", 0) for r in ok)),
            "requests": len(chunks)}


