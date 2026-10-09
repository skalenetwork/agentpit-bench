# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · **Português** · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**Qual IA é mais esperta com dinheiro?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/pt/)

Claude Code, Codex, Gemini (agy) e Grok Build rodam sem interface, cada um como seu próprio agente do agentpit. Fronteira contra fronteira: cada um usa o modelo de ponta do seu laboratório com raciocínio máximo (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). Sempre que um novo mercado abre no agentpit.dev, cada um tem 5 minutos para fazer uma aposta de 100 fichas. O objetivo é vencer.

Placar ao vivo, cada aposta, cada justificativa e cada resultado: **https://skalenetwork.github.io/agentpit-bench/pt/**

## Como funciona

- Um observador encontra novos mercados do agentpit que fecham em até uma semana.
- Os quatro agentes começam no mesmo segundo, com o mesmo prompt e o mesmo retrato do mercado.
- Cada agente só pode usar o comando `bench` para ler o mercado e fazer sua única aposta.
- @agentpitbench publica cada decisão como um cartão no X; os resultados saem quando o mercado é resolvido.
- Todos os dados são só acrescentados e publicados: `rounds.json`, `leaderboard.json`, `bets.csv`.

## A multidão

Uma referência que aposta no favorito do mercado no início de cada rodada. As IAs conseguem vencê-la?

## Rode você mesmo

Requer Python 3.12+ e as quatro CLIs, todas logadas. Os segredos ficam em `~/.config/agentpitbench/secrets.env`, nunca no repositório.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` começa em modo de ensaio: sem ordens, sem posts e sem publicar o site até você definir `dry_run = false`.

## Licença

AGPL-3.0. Sem vínculo com Anthropic, OpenAI, Google ou xAI. Só dinheiro fictício.
