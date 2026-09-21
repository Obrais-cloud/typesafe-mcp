"""jevkit — canonical Jev (TypeSafe System One) judgments shared across the fleet.

Every tool that judges with Jev — ollaeval, olladiff, ollarena, localeval,
promptcmp, pushdoctor, and this MCP server — should ask the SAME calibrated
question. Keeping the question *definitions* in one place is the whole point:
scores stay comparable across tools, and a wording fix lands everywhere at once.

Dependency-light on purpose: standard library only (urllib), so a zero-pip CLI
can `import jevkit` without pulling in httpx. The key is read from
$TYPESAFE_API_KEY or ~/typesafe-mcp/.env and is never logged.

Primitives exposed:
    quality_judge(prompt, response, criteria)  -> graded Score (one answer)
    compare_pair(prompt, a, b, criteria)       -> Choice winner (A / B / tie)
    classify_push_failure(error_text)          -> Choice cause + Noul + Score
    systemone(state, questions)                -> raw passthrough for anything else
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

TS_URL = os.environ.get("TS_MCP_TS_URL", "https://api.typesafe.ai/v1/systemone")
TS_MODEL = (os.environ.get("TYPESAFE_MODEL") or "jev-1.13.0")  # fijado: los alias se mueven, los umbrales no


# ── transport ───────────────────────────────────────────────────────────────
def key() -> str:
    """The TypeSafe API key from env, else ~/typesafe-mcp/.env. Never printed."""
    k = os.environ.get("TYPESAFE_API_KEY")
    if k:
        return k
    for path in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                 os.path.expanduser("~/typesafe-mcp/.env")):
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("TYPESAFE_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    raise RuntimeError("TYPESAFE_API_KEY not set (env or ~/typesafe-mcp/.env)")


def systemone(state, questions: dict, model: str = TS_MODEL, timeout: float = 60.0) -> dict:
    """Raw execute: {model, state, questions} -> full API response (answers, usage)."""
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        TS_URL, data=body,
        headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── 1. graded quality of a single answer (ollaeval, localeval, olladiff) ─────
QUALITY_LEVELS = [
    "Fails the criteria: wrong, off-topic, or missing what the prompt asked for.",
    "Partially meets the criteria: some correct elements but notable errors, gaps, or omissions.",
    "Mostly meets the criteria: largely correct and on target, with only minor issues.",
    "Fully meets the criteria: correct, complete, and directly satisfies everything asked.",
]
QUALITY_MAX = len(QUALITY_LEVELS) - 1
DEFAULT_QUALITY_CRITERIA = (
    "Overall quality: accuracy, completeness, and how well it answers the prompt."
)


def quality_judge(prompt: str, response: str, criteria: str | None = None,
                  model: str = TS_MODEL) -> dict:
    """Grade one response against `criteria`. Returns a normalized 0..1 `value`
    plus the raw score, confidence and full distribution."""
    resp = systemone(
        {"prompt": prompt, "evaluation_criteria": criteria or DEFAULT_QUALITY_CRITERIA,
         "response": response},
        {"quality": {"type": "score",
                     "instructions": "Judge how well the assistant's `response` to the `prompt` "
                                     "meets the `evaluation_criteria`. Judge only against the "
                                     "criteria; ignore style unless the criteria mention it.",
                     "criteria": QUALITY_LEVELS}},
        model=model,
    )
    a = resp["answers"]["quality"]
    top = max(a["probabilities"], key=a["probabilities"].get)
    return {
        "value": min(max(a["score"] / QUALITY_MAX, 0.0), 1.0),
        "raw_score": round(a["score"], 3),
        "max": QUALITY_MAX,
        "confidence": round(a["confidence"], 3),
        "level": a["legend"][top],
        "probabilities": {k: round(v, 3) for k, v in a["probabilities"].items()},
        "input_tokens": resp.get("usage", {}).get("input_tokens"),
    }


# ── 2. pairwise winner (ollarena ELO, olladiff-local, promptcmp) ─────────────
PAIR_OPTIONS = {
    "a": "Response A better satisfies the prompt and criteria.",
    "b": "Response B better satisfies the prompt and criteria.",
    "tie": "The two responses are of essentially equal quality for this prompt.",
}


def compare_pair(prompt: str, a: str, b: str, criteria: str | None = None,
                 model: str = TS_MODEL) -> dict:
    """Pick the better of two responses to the same prompt. Returns the winner
    ('a' | 'b' | 'tie'), confidence and the probability spread — feed the winner
    (or the probabilities) straight into an ELO update."""
    resp = systemone(
        {"prompt": prompt, "criteria": criteria or DEFAULT_QUALITY_CRITERIA,
         "response_a": a, "response_b": b},
        {"winner": {"type": "choice",
                    "instructions": "Which response better answers the `prompt` according to the "
                                    "`criteria`: `response_a`, `response_b`, or are they equal?",
                    "criteria": PAIR_OPTIONS}},
        model=model,
    )
    w = resp["answers"]["winner"]
    return {
        "winner": w["choice"],
        "confidence": round(w["confidence"], 3),
        "probabilities": {k: round(v, 3) for k, v in w["probabilities"].items()},
        "input_tokens": resp.get("usage", {}).get("input_tokens"),
    }


# ── 2b. correctness against a known answer (localeval) ───────────────────────
def answer_matches(prompt: str, response: str, expected, model: str = TS_MODEL) -> dict:
    """Judge whether `response` correctly satisfies a known `expected` answer.
    Returns a calibrated probability (`value`) and a pass/fail (`correct`).
    Use when ground truth exists — semantic correctness, not string equality."""
    exp = expected if isinstance(expected, str) else "\n".join(str(e) for e in expected)
    resp = systemone(
        {"prompt": prompt, "expected_answer": exp, "response": response},
        {"correct": {"type": "noul",
                     "instructions": "Does the `response` correctly satisfy the `expected_answer` "
                                     "to the `prompt`? Judge semantic correctness, not exact wording."}},
        model=model,
    )
    p = resp["answers"]["correct"]["noul"]
    return {"value": round(p, 3), "correct": p >= 0.5,
            "input_tokens": resp.get("usage", {}).get("input_tokens")}


# ── 2c. rank N responses to one prompt (promptcmp, ollarena N-way) ────────────
def best_of(prompt: str, named_responses: dict, criteria: str | None = None,
            tie_epsilon: float = 0.05, model: str = TS_MODEL) -> dict:
    """Grade each response (reference-free quality) and rank them. Returns the
    winner name (None on a tie), the ranked (name, value) list, and every score.
    N independent Score calls; for two candidates prefer `compare_pair`."""
    scores = {name: quality_judge(prompt, text, criteria, model=model)
              for name, text in named_responses.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1]["value"], reverse=True)
    tie = len(ranked) > 1 and (ranked[0][1]["value"] - ranked[1][1]["value"]) < tie_epsilon
    return {"winner": None if tie else ranked[0][0], "tie": tie,
            "ranked": [(n, s["value"]) for n, s in ranked], "scores": scores}


# ── 3. git push failure classifier (pushdoctor) ──────────────────────────────
PUSH_CAUSES = {
    "auth": "Authentication/credentials failure: missing or expired token, no credential "
            "helper, 'could not read Username', 'Authentication failed', HTTP 401 on write.",
    "no_write_permission": "Credentials work but the account lacks write access to this repo: "
            "403 'permission denied', read-only collaborator, or pushing to someone else's repo.",
    "protected_branch": "The target branch is protected: GH006, 'protected branch hook declined', "
            "required reviews or status checks block a direct push.",
    "non_fast_forward": "Rejected as non-fast-forward: the local branch is behind the remote and "
            "must fetch/pull/rebase before pushing ('! [rejected]', 'fetch first', 'tip is behind').",
    "large_file": "A file exceeds the remote's size limit (GH001 'this exceeds ... file size limit') "
            "and needs Git LFS or removal from history.",
    "lfs": "Git LFS problem: LFS not installed, a lock, or bandwidth/data quota exceeded.",
    "repo_not_found": "The remote repository does not exist or is not visible: 'repository not found', "
            "404, wrong owner/name.",
    "network": "Network/connectivity failure reaching the remote: 'could not resolve host', timeout, "
            "connection reset, proxy or TLS error.",
    "other": "None of the above, or the cause cannot be determined from this text.",
}
PUSH_FIXES = {
    "auth": ["gh auth login -h github.com && gh auth setup-git",
             "# or switch to SSH:  git remote set-url origin git@github.com:OWNER/REPO.git"],
    "no_write_permission": ["# ask an admin for write access, or push to a fork you own:",
                            "gh repo fork --remote"],
    "protected_branch": ["git switch -c my-change && git push -u origin my-change   # open a PR"],
    "non_fast_forward": ["git pull --rebase origin <branch>    # then: git push"],
    "large_file": ['git lfs track "*.<ext>" && git add .gitattributes && git commit --amend',
                   "# or strip the big file from history (git filter-repo) before pushing"],
    "lfs": ["git lfs install && git lfs push origin <branch>",
            "# check LFS data quota in GitHub billing settings"],
    "repo_not_found": ["git remote -v    # verify owner/name and access",
                       "# create the repo or fix the remote URL"],
    "network": ["# check VPN/proxy/DNS, then retry",
                "ssh -T git@github.com     # or: git ls-remote <url>"],
    "other": ["pushdoctor .    # run the full environment probe for a definitive verdict"],
}
PUSH_SEVERITY_LEVELS = [
    "Trivial: a retry or one-line config change fixes it.",
    "Minor: a quick local action (pull/rebase, set upstream) resolves it.",
    "Moderate: a deliberate setup step is needed — LFS, credential re-auth, or a new branch/PR.",
    "Severe: blocked by policy or permissions the developer cannot change alone.",
]
PUSH_SEV_MAX = len(PUSH_SEVERITY_LEVELS) - 1
PUSH_CONFIDENCE_FLOOR = 0.55


def classify_push_failure(error_text: str, model: str = TS_MODEL) -> dict:
    """Typed diagnosis of a git push failure from its error output."""
    resp = systemone(
        {"git_error": error_text},
        {"cause": {"type": "choice",
                   "instructions": "What is the root cause of this git push failure, judged from the "
                                   "git error output in `git_error`?",
                   "criteria": PUSH_CAUSES},
         "needs_history_rewrite": {"type": "noul",
                   "instructions": "Would fixing this safely require rewriting or force-pushing git history?"},
         "severity": {"type": "score",
                   "instructions": "How disruptive is this failure to the developer's immediate ability to push?",
                   "criteria": PUSH_SEVERITY_LEVELS}},
        model=model,
    )
    a = resp["answers"]
    cause = a["cause"]["choice"]
    return {
        "cause": cause,
        "cause_confidence": round(a["cause"]["confidence"], 3),
        "cause_probabilities": {k: round(v, 3) for k, v in a["cause"]["probabilities"].items()},
        "needs_history_rewrite": round(a["needs_history_rewrite"]["noul"], 3),
        "severity_score": round(a["severity"]["score"], 3),
        "severity_norm": round(a["severity"]["score"] / PUSH_SEV_MAX, 3),
        "severity_confidence": round(a["severity"]["confidence"], 3),
        "fixes": PUSH_FIXES.get(cause, PUSH_FIXES["other"]),
        "escalate": a["cause"]["confidence"] < PUSH_CONFIDENCE_FLOOR or cause == "other",
        "usage": resp.get("usage", {}),
    }
