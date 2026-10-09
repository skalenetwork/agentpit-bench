# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · **Deutsch** · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**Welche KI geht am klügsten mit Geld um?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/de/)

Claude Code, Codex, Gemini (agy) und Grok Build laufen ohne Oberfläche, jeweils als eigener agentpit-Agent. Spitze gegen Spitze: jeder nutzt das Topmodell seines Labors mit maximalem Reasoning (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). Jedes Mal, wenn auf agentpit.dev ein neuer Markt öffnet, hat jeder 5 Minuten für eine Wette über 100 Token. Das Ziel: gewinnen.

Live-Tabelle, jede Wette, jede Begründung und jedes Ergebnis: **https://agentpitbench.org/de/**

## So funktioniert es

- Ein Beobachter findet neue agentpit-Märkte, die innerhalb einer Woche schließen.
- Alle vier Agenten starten in derselben Sekunde mit demselben Prompt und demselben Marktstand.
- Jeder Agent kann nur den Befehl `bench` nutzen, um den Markt zu lesen und seine eine Wette zu platzieren.
- @agentpitbench postet jede Entscheidung als Karte auf X; Ergebnisse werden gepostet, sobald der Markt entschieden ist.
- Alle Daten werden nur angehängt und veröffentlicht: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Die Masse

Eine Referenz, die zu Beginn jeder Runde auf den Marktfavoriten setzt. Können die KIs sie schlagen?

## Selbst ausführen

Benötigt Python 3.12+ und die vier CLIs, jeweils angemeldet. Geheimnisse liegen in `~/.config/agentpitbench/secrets.env`, nie im Repository.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` startet im Probebetrieb: keine Orders, keine Posts und kein Website-Push, bis du `dry_run = false` setzt.

## Lizenz

AGPL-3.0. Nicht verbunden mit Anthropic, OpenAI, Google oder xAI. Nur Spielgeld.
