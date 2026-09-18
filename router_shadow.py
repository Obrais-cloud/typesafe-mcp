#!/usr/bin/env python3
"""
router-shadow — servicio HTTP que DECIDE a qué tier/host enrutaría cada prompt y lo
REGISTRA, sin enrutar ni ejecutar nada (modo sombra). Sirve para validar el router
sobre tráfico real antes de enchufarlo delante de ollaroute/ollabalance.

Endpoints:
  POST /route    {"prompt": "..."}  -> {tier,host,model,confidence,dist}; añade línea a router-shadow.jsonl
  GET  /summary                     -> distribución de tiers y ahorro estimado sobre lo acumulado
  GET  /health                      -> {"ok": true}

Puerto por defecto 11450 (env TS_ROUTER_SHADOW_PORT). No modifica ningún tráfico.
"""
import asyncio
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core
from fleet_optimize import TIERS, ROUTE_QUESTION

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "router-shadow.jsonl")
PORT = int(os.environ.get("TS_ROUTER_SHADOW_PORT", "11450"))


def decide(prompt: str) -> dict:
    res = asyncio.run(core.do_systemone(prompt, {"tier": ROUTE_QUESTION}))
    a = res["answers"]["tier"]
    t = TIERS[a["choice"]]
    return {"tier": a["choice"], "host": t["host"], "model": t["model"],
            "confidence": a["confidence"], "dist": a["probabilities"]}


def summarize() -> dict:
    counts, cost_routed, cost_heavy, n = {}, 0.0, 0.0, 0
    try:
        for line in open(LOG):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            tier = d.get("tier")
            if tier not in TIERS:
                continue
            counts[tier] = counts.get(tier, 0) + 1
            cost_routed += TIERS[tier]["cost"]
            cost_heavy += TIERS["heavy"]["cost"]
            n += 1
    except OSError:
        pass
    saving = round((1 - cost_routed / cost_heavy) * 100, 1) if cost_heavy else 0.0
    return {"decisiones": n, "por_tier": counts,
            "ahorro_computo_estimado_pct": saving,
            "coste_enrutado": round(cost_routed, 1),
            "coste_baseline_all_heavy": round(cost_heavy, 1)}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # silencio

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "shadow": True, "port": PORT})
        elif self.path == "/summary":
            self._send(200, summarize())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/route":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            prompt = json.loads(self.rfile.read(n) or b"{}").get("prompt", "")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad json"})
        if not prompt:
            return self._send(400, {"error": "prompt vacío"})
        try:
            d = decide(prompt)
        except Exception as e:
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "prompt": prompt[:200], **d}, ensure_ascii=False) + "\n")
        self._send(200, {**d, "shadow": True, "routed": False})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
