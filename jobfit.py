#!/usr/bin/env python3
"""
job_fit: run career-ops' typesafe-job-fit.mjs as a subprocess and return a
compact triage decision for the nightly employment routine.

career-ops lives on the MacBook and is a third-party MIT repo with a bespoke
`typesafe-job-fit.mjs` on top. This wrapper shells out to it with the REAL CLI
(`node typesafe-job-fit.mjs <postings.json> --json`) rather than reimplementing
its thresholds, which the file itself declares are the single review surface.

Every failure degrades to `{"decision": "manual", ...}` so the routine hands the
posting to a person instead of guessing. Timeout defaults to 20s.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import core
import ledger

CAREEROPS_DIR = os.environ.get("CAREEROPS_DIR", os.path.expanduser("~/career-ops"))
JOBFIT_SCRIPT = "typesafe-job-fit.mjs"
DEFAULT_TIMEOUT_S = 20.0


def _map_decision(action: str | None) -> str:
    """The mjs emits 4 actions; the tool exposes 3 decisions. surface_only is
    'hand it to a person', so it maps to joint_eval (never auto-acts)."""
    if action in ("joint_evaluation", "surface_only"):
        return "joint_eval"
    if action in ("auto_apply", "drop"):
        return action
    return "manual"


def _one(result: dict) -> dict:
    tokens = (result.get("usage") or {}).get("input_tokens", 0) or 0
    return {
        "decision": _map_decision(result.get("action")),
        "action": result.get("action"),
        "fit": result.get("fit"),
        "reasons": result.get("reasons", []),
        "cost_usd": round((tokens / 1e6) * 0.042, 6),
        "title": (result.get("posting") or {}).get("title"),
    }


async def run_job_fit(posting, timeout: float = DEFAULT_TIMEOUT_S,
                      careerops_dir: str = CAREEROPS_DIR):
    """posting: one {title, company?, location?, url?, body} dict, or a list.
    Returns one decision dict (single input) or a list (list input)."""
    script = os.path.join(careerops_dir, JOBFIT_SCRIPT)
    if not os.path.exists(script):
        return {"decision": "manual", "reason": f"job-fit script not found at {script}"}

    is_list = isinstance(posting, list)
    postings = posting if is_list else [posting]

    env = dict(os.environ)
    try:
        env.setdefault("TYPESAFE_API_KEY", core._key())
    except Exception as e:  # no key anywhere
        return {"decision": "manual", "reason": f"no TypeSafe key: {str(e)[:200]}"}
    # Pass ledger creds through if this machine has them, so the mjs records a
    # careerops.jobfit row. Absent → the mjs simply no-ops the ledger.
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        val = ledger._env(var)
        if val:
            env[var] = val

    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(postings, fh)
            path = fh.name
        proc = await asyncio.create_subprocess_exec(
            "node", script, path, "--json",
            cwd=careerops_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"decision": "manual", "reason": f"job-fit timed out after {timeout:g}s"}
    except Exception as e:
        return {"decision": "manual", "reason": f"job-fit could not start: {str(e)[:200]}"}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    if proc.returncode != 0:
        return {"decision": "manual",
                "reason": f"job-fit exited {proc.returncode}: {err.decode('utf-8', 'replace')[:300]}"}
    try:
        results = json.loads(out.decode("utf-8"))
    except Exception as e:
        return {"decision": "manual", "reason": f"could not parse job-fit output: {str(e)[:200]}"}

    mapped = [_one(r) for r in results]
    if is_list:
        return mapped
    return mapped[0] if mapped else {"decision": "manual", "reason": "no result returned"}
