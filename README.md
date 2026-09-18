# typesafe-mcp

MCP server que expone **TypeSafe** (Jev / System One) a la flota (openclaw + hermes).

## Herramientas

| Tool | Qué hace |
|---|---|
| `judge(text, content?, ts_model?)` | Lenguaje natural → compila (LLM de la flota) a request TypeSafe tipado → ejecuta. Devuelve el request + answers con probabilidades. |
| `rerank(items, criterion, ts_model?)` | Ordena una lista por un criterio en NL usando un **Score comparable** por item. `items`: `[{id,text}]` o strings. |
| `systemone(state, questions, ts_model?)` | Passthrough crudo: tú das `{state, questions}`, se ejecuta (sin LLM). |

`judge`/`rerank` usan la flota Ollama para compilar (qwen3.8:27b @ corsair, con fallback y wake vía ollawake). `systemone` no toca la flota.

## Ejecutar

```bash
export TYPESAFE_API_KEY=...        # requerido (usar SecretRef en la flota)
python3 server.py                 # MCP por stdio
```

Env opcionales: `TS_MCP_LLM_CHAIN` (JSON de la cadena de fallback), `TS_MCP_OLLAWAKE`.

## Estructura

- `core.py` — lógica (TypeSafe + compilador + fallback de flota + rerank), sin dependencia de `mcp`; testeable sola.
- `server.py` — wrapper MCP fino. Shim compatible: `mcp` v1 (`FastMCP`) y v2 (`MCPServer`).

## Notas

- Compilación robusta: `think:false` + strip `<think>` + reparación de JSON desbalanceado.
- `TYPESAFE_API_KEY` solo de entorno; nunca en disco ni en la imagen.

## Secreto (fleet)

La key se lee de `TYPESAFE_API_KEY` (env) o, si no está, de `~/typesafe-mcp/.env` (línea `TYPESAFE_API_KEY=...`). Así el registro en openclaw/hermes **no** lleva el secreto. `.env` está en `.gitignore`.
