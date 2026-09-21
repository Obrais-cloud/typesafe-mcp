#!/usr/bin/env bash
# Audita un repo contra el contrato TypeSafe v1 y las reglas de la casa.
# Uso: audit_typesafe.sh <ruta_repo> [<ruta_repo> ...]
# Salida: hallazgos por severidad. Exit 1 si hay algún BLOQUEANTE.
set -u
EXCL='--exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.next --exclude-dir=dist --exclude-dir=build --exclude-dir=.venv --exclude-dir=venv --exclude-dir=__pycache__'
INC='--include=*.py --include=*.ts --include=*.tsx --include=*.mts --include=*.mjs --include=*.js --include=*.cjs --include=*.json --include=*.toml --include=*.txt --include=*.cfg'
blockers=0
hit() { # sev etiqueta patron repo
  local out; out=$(grep -rnE $EXCL $INC "$3" "$4" 2>/dev/null | grep -v 'audit_typesafe' | head -20)
  if [ -n "$out" ]; then
    echo "[$1] $2"; echo "$out" | sed 's/^/    /'
    [ "$1" = "BLOQUEANTE" ] && blockers=$((blockers+1))
  fi
}
for repo in "$@"; do
  echo "=== $repo"
  [ -d "$repo" ] || { echo "  (no existe)"; continue; }
  # Contrato preview: roto contra la API actual
  hit BLOQUEANTE "endpoint preview" 'preview/evaluation' "$repo"
  hit BLOQUEANTE "paquete Python antiguo typesafe-client" 'typesafe[-_]client' "$repo"
  hit BLOQUEANTE "metodo antiguo evaluate() del cliente TypeSafe" '(client|typesafe|ts)[A-Za-z_]*\.evaluate(_async)?\(' "$repo"
  hit BLOQUEANTE "campo document en el body (v1 exige state)" '"document"[[:space:]]*:|document=' "$repo"
  hit BLOQUEANTE "array prompts (v1 usa mapa questions)" '"prompts"[[:space:]]*:|prompts=' "$repo"
  hit BLOQUEANTE "campos de respuesta preview" '\.(chosen|expectation)\b|\["(chosen|expectation)"\]|billing_units|"responses"' "$repo"
  hit AVISO "claves options/levels (bloqueante solo si llegan tal cual al API; v1 usa criteria)" '"(options|levels)"[[:space:]]*:[[:space:]]*\[' "$repo"
  # Riesgos de calibracion
  hit AVISO "alias jev-latest/jev-preview (fijar jev-1.13.0 en prod)" 'jev-(latest|preview)' "$repo"
  hit AVISO "umbral sobre confidence (revisar: masa de probabilidad si la barra es un nivel)" 'confidence[[:space:]]*[<>]=?' "$repo"
  hit AVISO "suma exacta de probabilidades en tests" '(sum|reduce).*probabilit.*(===|==)[[:space:]]*1(\.0)?\b' "$repo"
  hit INFO "referencia posicional items[i] (comprobar que los items NO llevan campo id)" '`[a-zA-Z_]+\[[^]]*\]\.' "$repo"
  hit INFO "version del SDK declarada" 'typesafe-sdk|@typesafe-ai/sdk' "$repo"
done
echo "--- bloqueantes: $blockers"
[ "$blockers" -eq 0 ]
