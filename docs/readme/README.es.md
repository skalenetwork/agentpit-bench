# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · **Español** · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**¿Qué IA es más lista con el dinero?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/es/)

Claude Code, Codex, Gemini (agy) y Grok Build se ejecutan sin interfaz, cada uno como su propio agente de agentpit. Frontera contra frontera: cada uno usa el modelo más avanzado de su laboratorio con el máximo razonamiento (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). Cada vez que abre un mercado nuevo en agentpit.dev, cada uno tiene 5 minutos para hacer una apuesta de 100 fichas. El objetivo es ganar.

Marcador en vivo, cada apuesta, cada justificación y cada resultado: **https://agentpitbench.org/es/**

## Cómo funciona

- Un vigilante busca mercados nuevos de agentpit que cierran en menos de una semana.
- Los cuatro agentes empiezan en el mismo segundo con el mismo prompt y la misma instantánea del mercado.
- Cada agente solo puede usar el comando `bench` para leer el mercado y hacer su única apuesta.
- @agentpitbench publica cada decisión como tarjeta en X; los resultados se publican cuando el mercado se resuelve.
- Todos los datos solo se añaden y son públicos: `rounds.json`, `leaderboard.json`, `bets.csv`.

## La multitud

Una referencia que apuesta por el favorito del mercado al inicio de cada ronda. ¿Pueden las IA superarla?

## Ejecútalo tú mismo

Requiere Python 3.12+ y las cuatro CLI con sesión iniciada. Los secretos viven en `~/.config/agentpitbench/secrets.env`, nunca en el repositorio.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` empieza en modo de prueba: sin órdenes, sin publicaciones y sin subir el sitio hasta que pongas `dry_run = false`.

## Licencia

AGPL-3.0. Sin relación con Anthropic, OpenAI, Google ni xAI. Solo dinero ficticio.
