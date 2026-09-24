# Integración de Jev en la operación diaria — 2026-09

Ejecución del brief `~/Downloads/jev-integracion-brief.md` en el MacBook Pro de Brais.
Este documento se completa a medida que avanzan los pasos. Empieza por los hallazgos
del Paso 0 (preflight), que es el gate para continuar.

Modelo fijado en todo: `jev-1.13.0` (nunca `jev-latest`).

---

## Paso 0 — Preflight (hallazgos, 2026-09-23)

### 1. Estado de los tres repos

| Repo | Rama | HEAD | Estado |
|------|------|------|--------|
| `~/typesafe-mcp` | `main` | `802bc95` (Ignore every runtime artifact…) | limpio, sincronizado con origin |
| `~/docs-mission-control` | `main` | `5b6a49d` (Add a TypeSafe (Jev) decision layer…) | limpio, sincronizado con origin |
| `~/career-ops` | `main` | `d6d680d` (Add TypeSafe (Jev) job-posting triage…) | `ahead 1` (commit local esperado); untracked `cv.md` y `career-ops/` |

- Los tres HEAD coinciden con lo verificado en el brief (`802bc95`, `5b6a49d`, y el
  commit local de job-fit en career-ops).
- career-ops: el `ahead 1` es el commit local de job-fit que el brief manda **no**
  pushear. Los untracked `cv.md` y la carpeta de datos `career-ops/` son exactamente
  los datos protegidos que el brief prohíbe commitear. **No son cambios ajenos
  inesperados** → no se dispara la condición de parada del Paso 0.

### 2. Mini desplegado

- `ssh macmini 'cat ~/typesafe-mcp/DEPLOYED_REV'` → `802bc95` ✓ (coincide).
- El mini guarda una copia plana (no un clon git); `deploy/deploy-mini.sh` es la vía
  soportada. Usuario `remotework`, host `macmini` (100.94.117.48).

### 3. Entrypoint y transporte del MCP

- **Entrypoint:** `~/typesafe-mcp/server.py`.
- **Transporte:** **stdio** (`mcp.run()` por defecto; el propio archivo dice
  "Run: python3 server.py (stdio MCP)").
- **Tools actuales:** `judge`, `rerank`, `systemone` (delegan en `core.py`) y las de
  `jevkit`: `quality_judge`, `compare`, `classify_push_failure`.
- **Punto único de llamada a la API TypeSafe:** `core.py:_ts_execute()` (línea 257).
  Todo `do_judge`/`do_rerank`/`do_systemone` pasa por ahí. Es el sitio para el insert
  del ledger en typesafe-mcp (Paso 1.3), aunque el `caller`/`pack`/`decision` se
  conocen en la capa `do_*`, así que el insert se hará con contexto pasado desde ahí.
- **Comando de arranque local (MacBook):** `python3 /Users/braisrevalderia/typesafe-mcp/server.py`.
  El Python del sistema (3.14, `/Library/Frameworks/Python.framework/.../bin/python3`)
  **ya importa `mcp` y `httpx`**. No hay `.venv` local (el `.venv` vive en el mini).

### 4. Interfaz real de `typesafe-job-fit.mjs` (career-ops)

- **Uso:** `TYPESAFE_API_KEY=... node typesafe-job-fit.mjs <posting.json|postings.json> [--json]`
- **Entrada:** archivo JSON con un posting `{ title, company?, location?, url?, body }`
  o un array de ellos.
- **Salida `--json`:** array de `{ posting, ...campos, action, reasons[] }` con, entre
  otros: `action`, `fit`, `fitConfidence`, `track`, `usage.input_tokens`, `model`,
  `harmSignals`, `clearance`, `weakestDimension`.
- **Acciones que emite (4):** `auto_apply`, `joint_evaluation`, `drop`, `surface_only`.
- **OJO discrepancia con el brief:** el Paso 2 pide que la tool `job_fit` devuelva
  `decision ∈ {auto_apply, joint_eval, drop}` (3 valores). El script emite 4. Mapeo que
  usaré en la tool: `auto_apply→auto_apply`, `joint_evaluation→joint_eval`,
  `drop→drop`, y `surface_only→joint_eval` (es "a decisión humana", nunca actúa solo),
  conservando el `action` original en un campo aparte para no perder información.
- `cost_usd` = `input_tokens / 1e6 * 0.042` (fórmula del propio script).
- Fallo de red/HTTP → la tool devolverá `decision: "manual"` + motivo (Paso 2.1).
- El archivo es autónomo a propósito (sin imports del resto del repo, que es MIT de
  terceros). `npm run update` no lo preserva.

### 5. Desde dónde corre la rutina nocturna de empleo

- **No hay** entrada en `crontab` ni LaunchAgent para career-ops/job-fit/scan.
- `claude_desktop_config.json` solo lista `~/career-ops`, `~/typesafe-mcp`,
  `~/docs-mission-control` como **folder grants** de Cowork (acceso de sesión), no como
  un servidor MCP ni como una tarea programada.
- **Conclusión:** la rutina la conduce **Claude (Cowork) en este MacBook**, no un cron
  — coincide con la premisa del Paso 2 ("La rutina la conduce Claude, no un cron").
  Por eso `job_fit` tiene que ser una tool MCP registrada en el MacBook (Paso 3), y como
  career-ops solo vive aquí, la instancia de `job_fit` corre local en el MacBook.

### Hallazgos extra relevantes para pasos siguientes

- **dmc no tiene mecanismo de migraciones en el repo** (sin `supabase/migrations`, sin
  `.sql`). La capa de datos es PostgREST directo (`lib/db.ts`, `fetch`, sin SDK), con RLS
  vía `mc_authorized()` + cabecera `x-mc-secret` (`MC_DB_SECRET`) y clave anónima.
  → El "mecanismo que ya usa el repo" para crear `jev_calls` es aplicar SQL directo al
  proyecto Supabase. Se hará con las tools MCP de Supabase (`apply_migration`).
- `jev_calls` pide RLS **solo `service_role`**, distinto del patrón anon+secret de dmc.
  Por tanto los inserts de dmc a `jev_calls` usarán `SUPABASE_SERVICE_ROLE_KEY`
  (a confirmar que está en el entorno Vercel; el Paso 1.3 lo saca de ahí para el mini).
- career-ops **no** tiene `@supabase/supabase-js`. El insert del ledger (Paso 1.4) se
  hará con `fetch` plano a PostgREST + service role key (mantiene el archivo autónomo,
  sin añadir dependencias al repo de terceros).
- **Paso 5.2:** `typesafe-drift.err.log` está en `~/fleet-diagnostics/`
  (`~/fleet-diagnostics/typesafe-drift.err.log`), generado por el LaunchAgent
  `com.brais.typesafe-drift-check.plist` (script `typesafe-skill-drift-check.sh`).
- Artefacto raro (no tocar): existe un fichero/dir `remotework@100.94.117.48` dentro de
  `~/typesafe-mcp` (probable scp mal escrito). El deploy ya lo excluye (`--exclude='remotework@*'`).

**Gate Paso 0: CUMPLIDO** — hallazgos escritos aquí antes de tocar nada.

---

## Paso 1 — Ledger de uso

Proyecto Supabase: **"Obrais-cloud's Project"** — ref `zrjkskqpxlfnjnxzvyre`,
`SUPABASE_URL=https://zrjkskqpxlfnjnxzvyre.supabase.co` (us-west-1, PG15).

### 1.1 Migración `jev_calls` — HECHO Y VERIFICADO

- Tabla creada vía Supabase MCP `apply_migration` (mecanismo directo que usa el repo).
  Columnas exactas del brief + índices `(caller, created_at desc)` y `(created_at desc)`.
- **RLS activado, sin políticas** → solo `service_role` (que salta RLS) inserta y lee;
  `anon`/`authenticated` revocados y bloqueados. Verificado: insert→select→delete OK.
- Guarda **solo metadatos**; `state_hash` es SHA-256, nunca el state ni texto de correos/ofertas.

### 1.2 docs-mission-control — CÓDIGO HECHO, gates verdes salvo verificación con key

- Nuevo `lib/typesafe/ledger.ts`: `recordJevCall()` fire-and-forget, timeout 2s,
  no-op sin `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`, nunca lanza; `hashState()` SHA-256.
- Punto único: `TypeSafeClient.ask()` inserta una fila por llamada real (éxito y fallo→fallback);
  `withTypesafe()` inserta la fila del caso "sin key" (Jev desactivado). No hay doble conteo.
- caller por punto de entrada (no por pack): `dmc.inbound`, `dmc.sweep`, `dmc.fit`, `dmc.documents`;
  `pack` = inbound/outcome/venue-fit/document-version. `decision`/`decision_mass` quedan null en
  esta capa genérica (los asks de dmc son multi-pregunta; la decisión única se registra donde existe,
  p. ej. `careerops.jobfit`).
- Gates dmc: **tsc 0, unit tests 30/30, next build OK**. eslint: rojo **preexistente** (10 errores
  `no-explicit-any` en `Claude outputs/_ab.mts` y `scripts/typesafe-*.mts`, ya rojo en HEAD, ajenos
  a este trabajo); mi código (`lib`, `app`) pasa eslint limpio. No amplío alcance a esos ficheros.

### 1.3 typesafe-mcp — CÓDIGO HECHO, gates verdes

- Nuevo `ledger.py` (mismo patrón `.env` que `_key`): `record_jev_call()` fire-and-forget (task async
  o `asyncio.run` si no hay loop), timeout 2s, no-op sin creds, nunca lanza; `hash_state()`.
- Punto único `core.py:_ts_execute()` registra éxito y fallo; caller `mcp.judge`/`mcp.rerank`/`mcp.systemone`.
- Test nuevo `test_ledger.py` (4 tests): incluye la garantía del brief — `_ts_execute` devuelve la
  respuesta **aunque `record_jev_call` lance**. Gates: imports OK, `unittest` jevkit(7)+ledger(4) OK,
  `tools/audit_typesafe.sh .` = **0 bloqueantes**.
- **Falta en el mini:** `~/typesafe-mcp/.env` solo tiene `TYPESAFE_API_KEY`; hay que añadir
  `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (Paso pendiente por la key, ver abajo).

### 1.4 career-ops — CÓDIGO HECHO

- `typesafe-job-fit.mjs` (autónomo, sin deps nuevas): cargador `.env` local inline + `recordJevCall()`
  fire-and-forget 2s + `hashState()`. `scorePosting()` registra fila `careerops.jobfit` (decision mapeada,
  `decision_mass = weakestClearance`, input_tokens, latency, state_hash). node --check OK, import limpio.
- Requiere `~/career-ops/.env` (no commiteado) con `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.

### 1.5 fleet_weekly.sh — HECHO

- Bloque "Uso de Jev (ledger, últimos 7 días)": tabla por caller con nº llamadas, % fallback y
  overrides humanos + fila total. Lee creds del `.env`. `zsh -n` OK, el bloque Python compila.

### BLOQUEANTE de verificación (Paso 1 gate) — `SUPABASE_SERVICE_ROLE_KEY`

- El brief asumía que estaba en el entorno Vercel; **no está** (Vercel de dmc solo tiene
  `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `MC_DB_SECRET`, `TYPESAFE_API_KEY`, `CRON_SECRET`).
- No es obtenible por herramienta: el MCP de Supabase solo expone claves publishable/anon;
  `vercel env pull` fue denegado por el clasificador (Production Reads).
- Sin esa key el ledger no escribe en ninguna máquina, así que **el gate "una llamada real por caller
  deja fila en jev_calls" queda pendiente** hasta tenerla, y no puedo añadirla a Vercel/mini/career-ops.
- El código es seguro sin ella (no-op), así que todo lo anterior ya está desplegable.

## Paso 2 — job_fit para la rutina nocturna — CÓDIGO HECHO Y PROBADO

- Nuevo `jobfit.py`: `run_job_fit(posting)` ejecuta `career-ops/typesafe-job-fit.mjs` por
  subprocess con su interfaz real (`node typesafe-job-fit.mjs <json> --json`), timeout 20s.
  Mapea las 4 acciones a decisión `auto_apply|joint_eval|drop`; cualquier fallo →
  `{"decision":"manual","reason":...}` (nunca auto-aplica). Pasa `TYPESAFE_API_KEY` (de
  `core._key()`, ya presente en el `.env` del MacBook — **no hace falta sacar la key del mini**)
  y las creds Supabase si existen, para que el mjs registre la fila `careerops.jobfit`.
- Tool `job_fit(posting: dict)` añadida en `server.py`.
- **Test de dos ofertas (llamadas Jev reales, ~$0.0001 c/u):**
  - "Head of Production" → `auto_apply`, fit 0.82 ✓
  - "Junior Staff Accountant" (fuera de banda) → `drop`, fit 0.01 ✓
- Gate pendiente por la key/registro: (a) responder desde sesión nueva con `/mcp` → tras el
  registro del Paso 3; (b) dejar fila `careerops.jobfit` → tras `SUPABASE_SERVICE_ROLE_KEY`.
- `typesafe` aún NO está en `claude mcp list` (registro = Paso 3).

## Paso 3 — typesafe-mcp como tool en todos los agentes

Topología (justificada): el MacBook ya tiene la key de TypeSafe local y career-ops solo vive
aquí, así que el MacBook corre una **instancia local completa** (la menos frágil, y da `job_fit`);
las demás máquinas van por **SSH-stdio al mini** para que la key siga solo en el mini.

| Objetivo | Estado | Detalle |
|---|---|---|
| MacBook Claude Code | ✔ registrado + **Connected** | `claude mcp add --scope user typesafe -- <py3.14> ~/typesafe-mcp/server.py` |
| MacBook Claude Desktop/Cowork | ✔ añadido (aplica al reiniciar Desktop) | `claude_desktop_config.json` (backup `.bak-*`; conserva `pencil`) |
| MacBook `~/.claude/CLAUDE.md` | ✔ bloque "Juicios con Jev" añadido | |
| Mac Studio Claude Code | ✔ registrado + **Connected** | SSH-stdio a `remotework@100.70.244.85` (tiene clave); CLAUDE.md block ✔ |
| mini Claude Code (local) | ✔ registrado + **Connected** | comando local `.venv/bin/python server.py`; CLAUDE.md block ✔ |
| **Hermes** | ✔ **YA registrado** | `~/.hermes/config.yaml` → `mcp_servers.typesafe` (mismo comando del mini). Las tools nuevas (job_fit, etc.) aparecen tras el redeploy del mini. |
| **openclaw** | ⏸ **REPORTADO, no tocado** | `mcp-config.json` tiene filesystem/github/brave-search/ollapix; **no** typesafe (fue podado, ver `.bak-mcpclean`). Añadirlo exige editar config + **reiniciar el gateway**, que la memoria marca como frágil (watchdog reinicia al tocar config, crash-loop, tmp-leak). Stop condition del brief → lo dejo para confirmar contigo. Entrada a añadir: igual que `ollapix` pero con `.venv/bin/python ~/typesafe-mcp/server.py`. |
| **alien18** (Windows) | ⏸ **REPORTADO** | Tiene Claude Code, pero `ssh remotework@mini` = `Permission denied (publickey)` → sin clave al mini. El brief prohíbe copiar la key → no registro. |
| **corsairai** (Windows) | ⏸ **REPORTADO** | Tiene Claude Code, pero `ssh remotework@mini` = `Host key verification failed` → sin acceso al mini. Igual: no copio la key. |

- Bloque "Juicios con Jev" añadido en CLAUDE.md de MacBook, Mac Studio y mini. En Hermes/openclaw
  NO edité su prompt de sistema (misma fragilidad de gateway) — pendiente de confirmar.
- **Verificación diferida** (por la key): "sesión nueva lista la tool" = OK (Connected en 3 máquinas);
  "una llamada deja fila en jev_calls" = pendiente de `SUPABASE_SERVICE_ROLE_KEY`.

## Paso 4 — docs-mission-control: más puntos de decisión — PENDIENTE (checkpoint)

- 4.1 (verificar que el correo entrante pasa por Jev → filas `dmc.inbound` en prod): el código ya
  etiqueta `dmc.inbound`; la verificación de filas está **diferida** (sin `SUPABASE_SERVICE_ROLE_KEY`).
- 4.2 (venue-fit al pegar un link): requiere puntuar **un venue contra varios proyectos activos**
  (forma inversa a `/api/fit`, que rankea venues para un proyecto) y decidir UX de "guardar fitScore".
  Feature nueva con decisión de diseño.
- 4.3 (pack de "siguiente paso" para candidaturas abiertas, patrón "respond to changing state" de
  `~/typesafe-mcp/skill/SKILL.md`): pack nuevo + Choice sobre conjunto cerrado en código + guardia de
  frescura (descartar si el estado cambió) + **UI nueva** (solo sugiere, nunca ejecuta) + tests.
- 4.4: gates + `npx vercel --prod` + sweep real. El deploy es **outward-facing** y la verificación
  del gate está diferida → conviene tu visto bueno antes de shipear superficies de decisión nuevas.

## Paso 5 — Router shadow — PARCIAL

- 5.1: `router_shadow.py` (`:11450`) es **pasivo** (responde `/route`, loguea; no muestrea). El punto
  que decide qué espejar NO está en el ollaroute del MacBook (sin refs a `:11450`/shadow). Hay que
  localizarlo en el ollaroute del **mini** (`:11435`) u otro proxy. Investigación en el mini pendiente;
  el brief dice reportar el punto exacto y no tocar código de ollaroute si no es configurable.
- 5.2: **HECHO** — `~/fleet-diagnostics/typesafe-drift.err.log` → `.old`.
- 5.3 (deploy del mini con `deploy/deploy-mini.sh`): pendiente; **requiere commit** de typesafe-mcp
  (el script rechaza cambios sin commitear). Activaría ledger + `job_fit` en el mini (ledger sigue
  no-op sin la key; `job_fit` en el mini devuelve `manual` porque career-ops no está allí — inocuo).

## Paso 6 — Cierre — PENDIENTE

- Commit local + push de typesafe-mcp y docs-mission-control a `main` (career-ops solo commit local).
- Consulta SQL de uso por caller (para el informe):
  `select caller, count(*) n, round(100.0*sum((fallback)::int)/count(*),1) pct_fallback,
   sum((human_override is not null)::int) overrides from public.jev_calls
   where created_at > now() - interval '7 days' group by caller order by n desc;`
- Tabla resumen caller/estado/verificado/fila-en-ledger: al final.
