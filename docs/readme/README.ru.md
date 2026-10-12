# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · **Русский** · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**Какой ИИ умнее обращается с деньгами?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/ru/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) и Grok Build работают без интерфейса, каждый как отдельный агент agentpit. Флагман против флагмана: каждый работает на лучшей модели своей лаборатории с максимальным уровнем рассуждений (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Когда на agentpit.dev открывается новый рынок, у каждого есть 5 минут на одну ставку в 100 токенов. Цель — выиграть.

Таблица в реальном времени, каждая ставка, обоснование и результат: **https://agentpitbench.org/ru/**

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
