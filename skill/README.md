# Skill oficial de TypeSafe, vendorizado

`SKILL.md` es copia literal de
`github.com/typesafe-ai/skills/skills/typesafe-ai/SKILL.md`
(plugin v0.5.7, commit 65a39f3, 12 sep 2026). MIT, licencia incluida.

Está aquí para que los agentes de la flota (openclaw, hermes) lo puedan leer sin
depender de la red ni de una instalación por máquina.

Su guía se ha aplicado ya en dos sitios de este repo:

- `COMPILER_SYSTEM` en `core.py` lleva ahora las reglas de diseño de preguntas
  del skill: niveles de Score como situaciones concretas e independientes, una
  sola dimensión por Score, opción de no-coincidencia en los Choice, un Noul por
  etiqueta cuando pueden aplicar varias, y nunca pedir al modelo algo que el
  código calcula exacto.
- `_validate_questions` comprueba los límites del contrato v1 (Score de 2 a 10
  niveles, Choice hasta 255 opciones, criteria de Noul solo `true`/`false`), que
  antes llegaban como un 422 desde el servicio.

Para actualizar:

    curl -sS -o skill/SKILL.md https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md

Instalarlo como plugin en una máquina con Claude Code:

    claude plugin marketplace add typesafe-ai/skills
    claude plugin install typesafe@typesafe-ai

Para otros agentes: `npx skills add typesafe-ai/skills --skill typesafe-ai`
