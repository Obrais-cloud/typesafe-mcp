#!/bin/zsh
# Informe semanal asesor de flota con TypeSafe (alerts + jobs + resources + router sombra).
# NO actúa: solo mide y escribe reports/weekly-<ts>.md
cd "$HOME/typesafe-mcp" || exit 1
PY=./.venv/bin/python
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p reports
OUT="reports/weekly-$TS.md"

{
  echo "# Informe semanal de flota (TypeSafe) — $TS"
  echo

  echo "## Alerts (severidad de eventos)"
  { for f in "$HOME"/.openclaw/logs/*.log "$HOME"/.hermes/logs/gateway*.log; do tail -60 "$f" 2>/dev/null; done; } \
    | grep -iE "error|warn|fail|panic|restart|timeout|reconnect|killed|crash|unreachable|degrade" \
    | grep -vE "INFO .*success|healthy" | tail -35 > /tmp/fw_lines.txt
  $PY fleet_optimize.py alerts --input /tmp/fw_lines.txt 2>&1
  echo

  echo "## Jobs (candidatos a revisar)"
  launchctl list | awk '$3 ~ /^(ai\.|com\.openclaw|com\.remotework|com\.suttonos)/ {print $3}' | sort -u > /tmp/fw_jobs.txt
  $PY fleet_optimize.py jobs --input /tmp/fw_jobs.txt 2>&1
  echo

  echo "## Resources (modelos cargados por host)"
  $PY - > /tmp/fw_hosts.json <<'PYEOF'
import json, urllib.request
hosts = {"corsair": "100.94.117.48", "mac-studio": "100.68.94.14", "alien18": "100.87.2.47"}
out = []
for h, ip in hosts.items():
    try:
        d = json.load(urllib.request.urlopen(f"http://{ip}:11434/api/ps", timeout=6))
        loaded = ", ".join(m["name"] for m in d.get("models", [])) or ""
    except Exception:
        loaded = "(no accesible)"
    out.append({"host": h, "loaded": loaded})
json.dump(out, open("/tmp/fw_hosts.json", "w"), ensure_ascii=False)
PYEOF
  $PY fleet_optimize.py resources --input /tmp/fw_hosts.json 2>&1
  echo

  echo "## Router (sombra acumulada)"
  curl -s -m 5 http://127.0.0.1:11450/summary 2>/dev/null || echo "(router-shadow no responde)"
  echo

  echo "## Uso de Jev (ledger, últimos 7 días)"
  $PY - <<'PYEOF' 2>&1
import json, os, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

def env(name):
    v = os.environ.get(name)
    if v:
        return v
    for p in (os.path.join(os.path.dirname(os.path.abspath("fleet_weekly.sh")), ".env"),
              os.path.expanduser("~/typesafe-mcp/.env")):
        try:
            for line in open(p):
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None

url, key = env("SUPABASE_URL"), env("SUPABASE_SERVICE_ROLE_KEY")
if not url or not key:
    print("(ledger no configurado en esta máquina: falta SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY)")
else:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    q = urllib.parse.urlencode({"select": "caller,fallback,human_override", "created_at": "gte." + since})
    req = urllib.request.Request(url.rstrip("/") + "/rest/v1/jev_calls?" + q,
                                 headers={"apikey": key, "Authorization": "Bearer " + key})
    try:
        rows = json.load(urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        rows = None
        print("(no se pudo leer el ledger:", str(e)[:120], ")")
    if rows is not None:
        agg = {}
        for r in rows:
            a = agg.setdefault(r.get("caller") or "?", {"n": 0, "fb": 0, "ov": 0})
            a["n"] += 1
            a["fb"] += 1 if r.get("fallback") else 0
            a["ov"] += 1 if r.get("human_override") else 0
        if not agg:
            print("Sin llamadas registradas en los últimos 7 días.")
        else:
            print("| caller | llamadas | % fallback | overrides humanos |")
            print("|---|---:|---:|---:|")
            for c in sorted(agg, key=lambda k: -agg[k]["n"]):
                a = agg[c]
                print(f"| {c} | {a['n']} | {100 * a['fb'] / a['n']:.0f}% | {a['ov']} |")
            tot = sum(a["n"] for a in agg.values())
            fb = sum(a["fb"] for a in agg.values())
            ov = sum(a["ov"] for a in agg.values())
            print(f"| **total** | **{tot}** | **{100 * fb / tot:.0f}%** | **{ov}** |")
PYEOF
  echo
} | tee "$OUT"

echo "$(date '+%Y-%m-%d %H:%M:%S')  weekly report -> $OUT" >> "$HOME/typesafe-mcp/fleet-weekly.log"
