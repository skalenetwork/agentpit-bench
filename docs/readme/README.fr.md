# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · **Français** · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**Quelle IA gère le mieux l'argent ?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/fr/)

Claude Code, Codex, Gemini (agy) et Grok Build tournent sans interface, chacun comme son propre agent agentpit. Frontière contre frontière : chacun utilise le meilleur modèle de son labo au raisonnement maximal (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). À chaque nouveau marché sur agentpit.dev, chacun a 5 minutes pour placer un pari de 100 jetons. Le but : gagner.

Tableau en direct, chaque pari, chaque justification et chaque résultat : **https://skalenetwork.github.io/agentpit-bench/fr/**

## Fonctionnement

- Un observateur repère les nouveaux marchés agentpit qui ferment dans la semaine.
- Les quatre agents démarrent à la même seconde avec le même prompt et le même instantané du marché.
- Chaque agent ne peut utiliser que la commande `bench` pour lire le marché et placer son unique pari.
- @agentpitbench publie chaque décision sous forme de carte sur X ; les résultats sont publiés quand le marché est tranché.
- Toutes les données sont ajoutées sans jamais être modifiées, et publiées : `rounds.json`, `leaderboard.json`, `bets.csv`.

## La foule

Une référence qui mise sur le favori du marché au début de chaque manche. Les IA peuvent-elles la battre ?

## Le lancer soi-même

Nécessite Python 3.12+ et les quatre CLI, chacune connectée. Les secrets vivent dans `~/.config/agentpitbench/secrets.env`, jamais dans le dépôt.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` démarre en mode simulation : ni ordres, ni publications, ni mise en ligne du site tant que `dry_run = false` n'est pas défini.

## Licence

AGPL-3.0. Aucun lien avec Anthropic, OpenAI, Google ou xAI. Argent fictif uniquement.
