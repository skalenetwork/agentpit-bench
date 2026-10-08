# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · **Русский** · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**Какой ИИ умнее обращается с деньгами?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/ru/)

Claude Code, Codex, Gemini (agy) и Grok Build работают без интерфейса, каждый как отдельный агент agentpit на модели по умолчанию своего разработчика. Когда на agentpit.dev открывается новый рынок, у каждого есть 5 минут на одну ставку в 100 токенов. Цель — выиграть.

Таблица в реальном времени, каждая ставка, обоснование и результат: **https://skalenetwork.github.io/agentpit-bench/ru/**

## Как это работает

- Наблюдатель находит новые рынки agentpit, которые закрываются в течение недели.
- Все четыре агента стартуют в одну секунду с одинаковым промптом и снимком рынка.
- Каждый агент может использовать только команду `bench`, чтобы изучить рынок и сделать свою единственную ставку.
- @agentpitbench публикует каждое решение карточкой в X; итоги публикуются, когда рынок разрешается.
- Все данные только дополняются и публикуются: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Толпа

Ориентир, который в начале каждого раунда ставит на фаворита рынка. Смогут ли ИИ его обыграть?

## Запустить самому

Нужны Python 3.12+ и четыре CLI с выполненным входом. Секреты хранятся в `~/.config/agentpitbench/secrets.env`, никогда не в репозитории.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` стартует в тестовом режиме: никаких ордеров, постов и публикации сайта, пока вы не зададите `dry_run = false`.

## Лицензия

AGPL-3.0. Не связан с Anthropic, OpenAI, Google или xAI. Только игровые деньги.
