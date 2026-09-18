#!/usr/bin/env python3
"""
Triage de cola local: rankea tickets por urgencia con TypeSafe y escribe un digest.

Flujo (idempotente, pensado para cron):
  queue/*.json     -> se leen (cada uno: {"id"?, "subject"?, "body"|"text"})
  -> core.do_rerank por urgencia
  -> digests/triage-<ts>.md  +  digests/latest.json
  -> los que obtuvieron score se mueven a processed/ (los que fallaron quedan para reintentar)
Cola vacía => no hace nada. Si TypeSafe/flota caen => deja la cola intacta y lo registra.
"""
import asyncio
import glob
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import core  # noqa: E402

QUEUE = os.path.join(BASE, "queue")
PROCESSED = os.path.join(BASE, "processed")
DIGESTS = os.path.join(BASE, "digests")
LOG = os.path.join(BASE, "triage.log")
CRITERION = "urgencia para el equipo de soporte, de no urgente a crítico"


def log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")


def load_queue() -> list:
    items = []
    for p in sorted(glob.glob(os.path.join(QUEUE, "*.json"))):
        try:
            d = json.load(open(p))
        except (json.JSONDecodeError, OSError) as e:
            log(f"skip {os.path.basename(p)}: json inválido ({e})")
            continue
        items.append({
            "_path": p,
            "id": str(d.get("id") or os.path.splitext(os.path.basename(p))[0]),
            "subject": d.get("subject", ""),
            "body": d.get("body") or d.get("text") or "",
        })
    return items


async def main() -> int:
    for d in (QUEUE, PROCESSED, DIGESTS):
        os.makedirs(d, exist_ok=True)
    items = load_queue()
    if not items:
        return 0

    payload = [{"id": it["id"], "text": (it["subject"] + "\n" + it["body"]).strip()} for it in items]
    try:
        res = await core.do_rerank(payload, CRITERION)
    except Exception as e:  # flota/API caída: no perder la cola
        log(f"rerank FALLÓ, cola intacta: {type(e).__name__} {e}")
        return 1

    ranked = res["ranked"]
    byid = {it["id"]: it for it in items}
    ts = time.strftime("%Y%m%d-%H%M%S")

    lines = [f"# Triage de urgencia — {ts}", "",
             f"Criterio: {CRITERION}",
             f"Niveles: {len(res['levels'])} · items: {len(items)} · tokens: {res['total_tokens']}", "",
             "| # | id | score | norm | conf | asunto |",
             "|---|---|------:|-----:|-----:|--------|"]
    for i, r in enumerate(ranked, 1):
        subj = byid.get(r["id"], {}).get("subject", "")[:44]
        if "error" in r:
            lines.append(f"| {i} | {r['id']} | ERR | | | {subj} |")
        else:
            lines.append(f"| {i} | {r['id']} | {r['score']:.2f} | {r['norm']:.2f} | {r['confidence']:.2f} | {subj} |")
    md = "\n".join(lines) + "\n"
    open(os.path.join(DIGESTS, f"triage-{ts}.md"), "w").write(md)
    json.dump({"ts": ts, "criterion": CRITERION, "levels": res["levels"], "ranked": ranked},
              open(os.path.join(DIGESTS, "latest.json"), "w"), ensure_ascii=False, indent=2)

    scored = {r["id"] for r in ranked if "error" not in r}
    moved = 0
    for it in items:
        if it["id"] in scored:
            shutil.move(it["_path"], os.path.join(PROCESSED, f"{ts}-{os.path.basename(it['_path'])}"))
            moved += 1

    top = next((r for r in ranked if "error" not in r), None)
    flag = " ⚠️ATENCIÓN(norm≥0.75)" if top and top.get("norm", 0) >= 0.75 else ""
    log(f"procesados {moved}/{len(items)} · top={top['id'] if top else '-'} "
        f"(norm={top['norm'] if top else '-'}) · digest triage-{ts}.md{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
