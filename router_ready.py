#!/usr/bin/env python3
"""
router-ready — ¿hay datos suficientes/sanos en la sombra para activar el router?

Lee router-shadow.jsonl (decisiones reales que ollaroute espejó) y evalúa un gate.
En --monitor: registra en router-ready.log y, si se cumple, escribe ROUTER-READY.flag.
NO activa nada (activar = cambiar el routing de ollaroute, decisión humana).
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "router-shadow.jsonl")
FLAG = os.path.join(BASE, "ROUTER-READY.flag")
READYLOG = os.path.join(BASE, "router-ready.log")
MSGFILE = os.path.join(BASE, "ROUTER-READY.msg.txt")
TELEGRAM = os.path.expanduser("~/.openclaw/scripts/send-telegram-summary.sh")


TELEGRAM_TARGET = os.environ.get("TS_TELEGRAM_TARGET", "1382220688")


def _notify(r):
    """Best-effort: avisa a Brais por Telegram (openclaw) al saltar el flag."""
    txt = ("\U0001F7E2 TypeSafe router LISTO para activar (datos suficientes en la sombra).\n"
           f"decisiones={r['n']} | dias={r['dias']} | tiers={r['tiers']}\n"
           f"conf_mediana={r['conf_mediana']} | baja_conf={r['baja_conf_pct']}%\n"
           "Siguiente paso: revisar y activar el routing de ollaroute por TypeSafe.")
    try:
        open(MSGFILE, "w").write(txt)
    except OSError:
        pass
    env = dict(os.environ)
    env["PATH"] = ("/opt/homebrew/bin:" + os.path.expanduser("~/.hermes/node/bin")
                   + ":" + env.get("PATH", ""))
    try:  # openclaw directo (ruta verificada), con freno de tiempo
        subprocess.run(["openclaw", "message", "send", "--channel", "telegram",
                        "--target", TELEGRAM_TARGET, "--message", txt, "--json"],
                       env=env, timeout=50, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

TH = {
    "min_decisiones": 200,        # volumen de tráfico real
    "min_dias": 5,                # repartido en el tiempo, no un pico
    "min_tiers": 2,               # no colapsa todo a un tier
    "min_conf_mediana": 0.70,     # decisiones confiadas
    "max_baja_conf_pct": 25.0,    # pocas decisiones muy dudosas (conf<0.5)
}


def load():
    rows = []
    try:
        for line in open(LOG):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows


def evaluate(rows):
    n = len(rows)
    days = len({r.get("ts", "")[:10] for r in rows if r.get("ts")})
    tiers, confs = {}, []
    for r in rows:
        t = r.get("tier")
        if t:
            tiers[t] = tiers.get(t, 0) + 1
        c = r.get("confidence")
        if isinstance(c, (int, float)):
            confs.append(c)
    confs.sort()
    med = confs[len(confs) // 2] if confs else 0.0
    lowpct = (100.0 * sum(1 for c in confs if c < 0.5) / len(confs)) if confs else 100.0
    checks = {
        "decisiones": (n, n >= TH["min_decisiones"]),
        "dias_distintos": (days, days >= TH["min_dias"]),
        "tiers_usados": (len(tiers), len(tiers) >= TH["min_tiers"]),
        "conf_mediana": (round(med, 2), med >= TH["min_conf_mediana"]),
        "baja_conf_pct": (round(lowpct, 1), lowpct <= TH["max_baja_conf_pct"]),
    }
    return {"ready": all(ok for _, ok in checks.values()), "n": n, "dias": days,
            "tiers": tiers, "conf_mediana": round(med, 2), "baja_conf_pct": round(lowpct, 1),
            "checks": checks, "umbrales": TH}


def main():
    r = evaluate(load())
    print(f"Router activación: {'READY ✅' if r['ready'] else 'aún NO'}")
    for k, (val, ok) in r["checks"].items():
        print(f"  [{'x' if ok else ' '}] {k}: {val}")
    print(f"  tiers: {r['tiers']}")
    if "--monitor" in sys.argv:
        open(READYLOG, "a").write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}  ready={r['ready']} n={r['n']} "
            f"dias={r['dias']} conf_med={r['conf_mediana']} baja%={r['baja_conf_pct']}\n")
        if r["ready"] and not os.path.exists(FLAG):
            open(FLAG, "w").write(json.dumps(r, ensure_ascii=False, indent=2))
            _notify(r)  # avisa una sola vez, al crear el flag
    # Under launchd (--monitor) "not ready yet" is the normal daily outcome, not
    # a failure; exiting 1 made the job look broken in `launchctl list`. An
    # interactive run still exits 1 so scripts can branch on readiness.
    if "--monitor" in sys.argv:
        return 0
    return 0 if r["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
