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
} | tee "$OUT"

echo "$(date '+%Y-%m-%d %H:%M:%S')  weekly report -> $OUT" >> "$HOME/typesafe-mcp/fleet-weekly.log"
