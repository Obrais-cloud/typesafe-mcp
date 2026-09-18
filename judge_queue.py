#!/usr/bin/env python3
"""
judge-queue — motor genérico: aplica un JUICIO TypeSafe a una cola de items.

Un solo motor, muchos dominios. Cada juicio (JUDGMENTS) define su primitivo y rúbrica.
  queue/<judgment>/*.json  (items {id?, subject?, body|text})
   -> score  => rankea y escribe digest
   -> noul   => marca (flag) los que superan umbral
   -> digests/<judgment>-<ts>.md (+ latest) ; archiva a processed/<judgment>/ ; log

Uso: python3 judge_queue.py <judgment>        (o 'all' para todos con cola)
Añadir un dominio = una entrada en JUDGMENTS. Sin LLM (rúbricas fijas), solo TypeSafe.
"""
import asyncio
import glob
import json
import os
import shutil
import sys
import time

import httpx

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import core  # noqa: E402

QUEUE = os.path.join(BASE, "queue")
PROCESSED = os.path.join(BASE, "processed")
DIGESTS = os.path.join(BASE, "digests")
LOG = os.path.join(BASE, "judge-queue.log")

# --- Registro de juicios (aquí se "aplica a todo": añade dominios) -----------
JUDGMENTS = {
    "urgency": {
        "kind": "score",
        "instructions": "¿Cómo de urgente es este ticket de soporte para que el equipo actúe?",
        "levels": [
            "No urgente: consulta general o feedback sin impacto ni prisa.",
            "Baja: inconveniente menor o cosmético; el cliente sigue operando normal.",
            "Media: función rota pero con workaround; molesto pero operativo.",
            "Alta: bloqueo sin workaround, o pérdida activa de dinero/datos.",
            "Crítica: caída total, brecha de seguridad/datos, o riesgo legal inmediato.",
        ],
        "flag": 0.75,  # norm
    },
    "virality": {
        "kind": "score",
        "instructions": "¿Qué potencial de enganche/viralidad tiene esta idea o gancho de contenido para redes?",
        "levels": [
            "Flojo: genérico, sin gancho ni ángulo; pasa desapercibido.",
            "Correcto: tema válido pero ejecución plana; engagement discreto.",
            "Bueno: gancho claro o ángulo fresco; probable buen alcance.",
            "Fuerte: gancho muy compartible, emocional o sorprendente; alto potencial viral.",
        ],
        "flag": 0.66,
    },
    "guardrail": {
        "kind": "noul",
        "instructions": ("¿Este output de un agente es ARRIESGADO para enviar/publicar tal cual? "
                         "Sí si contiene: una afirmación factual no verificada presentada como hecho, "
                         "un compromiso/promesa vinculante, datos personales o secretos, un tono off-brand "
                         "o agresivo, o instruye una acción irreversible. No si es informativo, acotado y seguro."),
        "criteria": {"true": "Arriesgado: revisar antes de enviar.",
                     "false": "Seguro para enviar tal cual."},
        "flag": 0.5,
    },
}


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")


def load(qdir):
    items = []
    for p in sorted(glob.glob(os.path.join(qdir, "*.json"))):
        try:
            d = json.load(open(p))
        except (json.JSONDecodeError, OSError) as e:
            log(f"skip {os.path.basename(p)}: {e}"); continue
        items.append({"_path": p,
                      "id": str(d.get("id") or os.path.splitext(os.path.basename(p))[0]),
                      "subject": d.get("subject", ""),
                      "text": (str(d.get("subject", "")) + "\n" + str(d.get("body") or d.get("text") or "")).strip()})
    return items


async def _batch_noul(items, question):
    async with httpx.AsyncClient() as client:
        async def one(it):
            req = {"model": core.TS_MODEL_DEFAULT, "state": it["text"], "questions": {"q": question}}
            try:
                r = await core._ts_execute(client, req)
                return {**it, "p": round(r["answers"]["q"]["noul"], 3),
                        "tokens": r.get("usage", {}).get("input_tokens", 0)}
            except httpx.HTTPError as e:
                return {**it, "error": str(e)}
        return await asyncio.gather(*[one(x) for x in items])


async def run_one(name):
    j = JUDGMENTS[name]
    qdir = os.path.join(QUEUE, name)
    os.makedirs(qdir, exist_ok=True)
    items = load(qdir)
    if not items:
        return None
    for d in (os.path.join(PROCESSED, name), DIGESTS):
        os.makedirs(d, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    try:
        if j["kind"] == "score":
            res = await core.do_rerank([{"id": it["id"], "text": it["text"]} for it in items],
                                       name, levels=j["levels"], instructions=j["instructions"])
            rows = res["ranked"]
            scored = {r["id"] for r in rows if "error" not in r}
            head = ["| # | id | score | norm | flag | asunto |", "|---|---|--:|--:|:--:|---|"]
            body = []
            for i, r in enumerate(rows, 1):
                if "error" in r:
                    body.append(f"| {i} | {r['id']} | ERR | | | |"); continue
                fl = "⚠️" if r["norm"] >= j["flag"] else ""
                subj = next((it["subject"] for it in items if it["id"] == r["id"]), "")[:40]
                body.append(f"| {i} | {r['id']} | {r['score']:.2f} | {r['norm']:.2f} | {fl} | {subj} |")
            top = rows[0] if rows and "error" not in rows[0] else None
            summary = f"top={top['id'] if top else '-'} (norm={top['norm'] if top else '-'})"
        else:  # noul
            q = {"type": "noul", "instructions": j["instructions"], "criteria": j["criteria"]}
            res = await _batch_noul(items, q)
            ok = [r for r in res if "error" not in r]
            ok.sort(key=lambda r: r["p"], reverse=True)
            scored = {r["id"] for r in ok}
            flagged = [r for r in ok if r["p"] >= j["flag"]]
            head = ["| id | p | flag | asunto |", "|---|--:|:--:|---|"]
            body = [f"| {r['id']} | {r['p']:.2f} | {'⚠️' if r['p'] >= j['flag'] else ''} | "
                    f"{next((it['subject'] for it in items if it['id']==r['id']),'')[:40]} |" for r in ok]
            summary = f"{len(flagged)}/{len(ok)} marcados (⚠️ p≥{j['flag']})"
    except Exception as e:
        log(f"[{name}] FALLÓ, cola intacta: {type(e).__name__} {e}")
        return f"{name}: ERROR {e}"

    md = f"# {name} — {ts}\n\n{j['instructions']}\n\n" + "\n".join(head + body) + "\n"
    open(os.path.join(DIGESTS, f"{name}-{ts}.md"), "w").write(md)
    open(os.path.join(DIGESTS, f"{name}-latest.md"), "w").write(md)
    moved = 0
    for it in items:
        if it["id"] in scored:
            shutil.move(it["_path"], os.path.join(PROCESSED, name, f"{ts}-{os.path.basename(it['_path'])}"))
            moved += 1
    log(f"[{name}] procesados {moved}/{len(items)} · {summary}")
    return f"{name}: {summary} ({moved} items)"


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(JUDGMENTS) if which == "all" else [which]
    if which != "all" and which not in JUDGMENTS:
        print(f"juicio desconocido: {which}. Disponibles: {', '.join(JUDGMENTS)}"); return 2
    out = []
    for n in names:
        r = await run_one(n)
        if r:
            out.append(r)
    print("\n".join(out) if out else "colas vacías (nada que procesar)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
