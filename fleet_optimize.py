#!/usr/bin/env python3
"""
fleet-optimize — TypeSafe como cerebro que PUNTÚA/PROPONE optimizaciones de la flota.
Modo asesor: mide y recomienda, NO actúa sobre producción (no desvía tráfico, no
retira jobs, no duerme hosts). Cada modo escribe un informe en reports/.

Modos:
  route      enruta peticiones al tier/host más barato-capaz (Choice) — eval en sombra
  alerts     clasifica eventos de fleet-health por severidad real (Choice+Noul) — denoise
  jobs       clasifica jobs/crons y marca candidatos a revisar (Choice+Noul)
  resources  juzga por host si está infrautilizado / candidato a dormir (Noul)
Uso: python3 fleet_optimize.py <modo> [--input fichero]
"""
import asyncio
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import core  # noqa: E402

REPORTS = os.path.join(BASE, "reports")


def _report(mode: str, data: dict) -> str:
    os.makedirs(REPORTS, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    p = os.path.join(REPORTS, f"{mode}-{ts}.json")
    json.dump(data, open(p, "w"), ensure_ascii=False, indent=2)
    return p


# ===== MODO ROUTER =========================================================
# Tiers reales de la flota (coste relativo aproximado por token de cómputo).
TIERS = {
    "light": {"host": "alien18", "model": "qwen3:14b", "cost": 1.0,
              "desc": "Tareas triviales/directas: saludos, clasificación simple, extracción de un dato, respuestas cortas factuales."},
    "mid":   {"host": "corsair", "model": "qwen3.6:35b-a3b", "cost": 1.8,
              "desc": "Tareas moderadas: resúmenes, reescrituras, Q&A con algo de contexto, clasificación matizada."},
    "heavy": {"host": "corsair", "model": "qwen3.8:27b", "cost": 3.0,
              "desc": "Tareas complejas: razonamiento multi-paso, código no trivial, análisis o planificación con dependencias."},
}
ROUTE_QUESTION = {
    "type": "choice",
    "instructions": "¿Qué nivel de modelo necesita esta petición para resolverse bien, minimizando cómputo?",
    "criteria": {k: v["desc"] for k, v in TIERS.items()},
}
SAMPLE_REQUESTS = [
    "¿Cuál es la capital de Francia?",
    "Clasifica el sentimiento de: 'me encanta este producto'",
    "Resume en 3 frases este email de un cliente pidiendo una devolución.",
    "Reescribe este párrafo en tono más formal.",
    "Escribe una función en Python que deduplique una lista preservando el orden y con tests.",
    "Diseña la arquitectura de un pipeline de ingest con reintentos, backoff y dead-letter queue, y justifica las decisiones.",
    "Traduce 'buenos días' al inglés.",
    "Explica paso a paso por qué falla este deadlock entre dos locks y cómo arreglarlo.",
]


async def mode_route(requests: list) -> dict:
    routed = []
    total_baseline = total_routed = 0.0
    for req in requests:
        res = await core.do_systemone(req, {"tier": ROUTE_QUESTION})
        a = res["answers"]["tier"]
        tier = a["choice"]
        t = TIERS[tier]
        total_baseline += TIERS["heavy"]["cost"]     # baseline: todo al 27B
        total_routed += t["cost"]
        routed.append({"request": req[:70], "tier": tier, "host": t["host"],
                       "model": t["model"], "confidence": a["confidence"],
                       "dist": a["probabilities"]})
    saving = 1 - total_routed / total_baseline if total_baseline else 0
    return {"routed": routed, "cost_routed": round(total_routed, 1),
            "cost_baseline_all_heavy": round(total_baseline, 1),
            "estimated_compute_saving_pct": round(saving * 100, 1)}


# ===== MODO ALERTS =========================================================
ALERT_SEVERITY = {
    "type": "score",
    "instructions": "¿Qué severidad operativa real tiene esta línea de log/alerta de la flota?",
    "criteria": [
        "Ruido: informativo, heartbeat, debug o éxito rutinario; ignorable.",
        "Aviso: anomalía menor o transitoria que conviene vigilar pero no requiere acción inmediata.",
        "Serio: fallo de un servicio/host o reintento persistente que degrada la flota; requiere atención.",
        "Crítico: caída, pérdida de datos, crash-loop o riesgo inmediato; actuar ya.",
    ],
}


async def mode_alerts(lines: list) -> dict:
    lines = [l for l in lines if l.strip()][:40]
    payload = [{"id": str(i), "text": l[:400]} for i, l in enumerate(lines)]
    res = await core.do_rerank(payload, "severidad operativa",
                               levels=ALERT_SEVERITY["criteria"],
                               instructions=ALERT_SEVERITY["instructions"])
    ranked = res["ranked"]
    byid = {str(i): l for i, l in enumerate(lines)}
    out = [{"severity_score": r.get("score"), "norm": r.get("norm"),
            "line": byid.get(r["id"], "")[:160]} for r in ranked if "error" not in r]
    actionable = [o for o in out if (o["norm"] or 0) >= 0.5]
    return {"total": len(out), "actionable": len(actionable),
            "noise_filtered": len(out) - len(actionable), "top": actionable[:15]}


# ===== MODO JOBS ===========================================================
import httpx  # noqa: E402

JOB_REVIEW = {
    "type": "noul",
    "instructions": ("Dado el NOMBRE de un job/cron de una flota de automatización, ¿parece un "
                     "candidato a revisar/retirar? Señales de sí: nombre con 'canary', 'test', "
                     "'tmp', 'old', 'bak', 'v2/v3', una fecha antigua embebida, 'experiment', o "
                     "duplicado evidente. Señales de no: watchdog, gateway, health, backup, "
                     "core/infra estable."),
    "criteria": {"true": "El nombre sugiere algo temporal, experimental, duplicado o obsoleto.",
                 "false": "El nombre sugiere infraestructura estable y necesaria."},
}


async def _batch_noul(client, items, question, ts_model):
    async def one(it):
        req = {"model": ts_model, "state": it["text"], "questions": {"q": question}}
        try:
            r = await core._ts_execute(client, req)
            return {**it, "p": round(r["answers"]["q"]["noul"], 3)}
        except httpx.HTTPError as e:
            return {**it, "error": str(e)}
    return await asyncio.gather(*[one(x) for x in items])


async def mode_jobs(labels: list) -> dict:
    items = [{"id": l, "text": l} for l in labels if l.strip()]
    async with httpx.AsyncClient() as client:
        scored = await _batch_noul(client, items, JOB_REVIEW, core.TS_MODEL_DEFAULT)
    ok = [s for s in scored if "error" not in s]
    ok.sort(key=lambda s: s["p"], reverse=True)
    flagged = [s for s in ok if s["p"] >= 0.6]
    return {"total": len(ok), "candidatos_revisar": len(flagged),
            "top": [{"job": s["id"], "p_revisar": s["p"]} for s in flagged[:25]]}


# ===== MODO RESOURCES ======================================================
RES_IDLE = {
    "type": "noul",
    "instructions": ("Dado un host de la flota y los modelos LLM que tiene CARGADOS en memoria "
                     "ahora mismo, ¿es candidato a liberar memoria (descargar modelos) por parecer "
                     "infrautilizado? Sí si hay modelos grandes cargados y el host no es el primario "
                     "de servicio. No si no hay nada cargado o es un host crítico siempre activo."),
    "criteria": {"true": "Modelos grandes cargados que probablemente no se están usando.",
                 "false": "Sin modelos cargados, o host crítico que debe seguir caliente."},
}


async def mode_resources(hosts: list) -> dict:
    items = [{"id": h["host"], "text": f"host={h['host']} cargados={h.get('loaded') or 'ninguno'}"}
             for h in hosts]
    async with httpx.AsyncClient() as client:
        scored = await _batch_noul(client, items, RES_IDLE, core.TS_MODEL_DEFAULT)
    ok = [s for s in scored if "error" not in s]
    ok.sort(key=lambda s: s["p"], reverse=True)
    return {"hosts": [{"host": s["id"], "p_liberar": s["p"], "detalle": s["text"]} for s in ok]}


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    inp = None
    if "--input" in sys.argv:
        inp = sys.argv[sys.argv.index("--input") + 1]

    if mode == "route":
        reqs = SAMPLE_REQUESTS
        if inp:
            reqs = [l.strip() for l in open(inp) if l.strip()]
        data = await mode_route(reqs)
        print(f"Router: coste enrutado {data['cost_routed']} vs baseline-27B "
              f"{data['cost_baseline_all_heavy']} → ahorro cómputo ~{data['estimated_compute_saving_pct']}%")
        for r in data["routed"]:
            print(f"  [{r['tier']:5}] {r['host']:9} {r['model']:16} conf={r['confidence']:.2f}  {r['request']}")
        print("informe:", _report("route", data))

    elif mode == "alerts":
        lines = [l.rstrip() for l in open(inp)] if inp else [l for l in sys.stdin]
        data = await mode_alerts(lines)
        print(f"Alerts: {data['total']} eventos → {data['actionable']} accionables, "
              f"{data['noise_filtered']} ruido filtrado")
        for o in data["top"]:
            print(f"  {o['norm']:.2f}  {o['line']}")
        print("informe:", _report("alerts", data))

    elif mode == "jobs":
        labels = [l.strip() for l in (open(inp) if inp else sys.stdin) if l.strip()]
        data = await mode_jobs(labels)
        print(f"Jobs: {data['total']} analizados → {data['candidatos_revisar']} candidatos a revisar")
        for j in data["top"]:
            print(f"  {j['p_revisar']:.2f}  {j['job']}")
        print("informe:", _report("jobs", data))

    elif mode == "resources":
        hosts = json.load(open(inp)) if inp else json.load(sys.stdin)
        data = await mode_resources(hosts)
        print("Resources (candidatos a liberar memoria, mayor p primero):")
        for h in data["hosts"]:
            print(f"  {h['p_liberar']:.2f}  {h['host']:12} {h['detalle']}")
        print("informe:", _report("resources", data))

    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
