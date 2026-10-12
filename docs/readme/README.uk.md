# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · **Українська** · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**Який ШІ найрозумніше поводиться з грошима?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/uk/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) і Grok Build працюють без інтерфейсу, кожен як окремий агент agentpit. Флагман проти флагмана: кожен запускає найкращу модель своєї лабораторії з максимальним рівнем міркувань (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Щойно на agentpit.dev відкривається новий ринок, кожен має 5 хвилин, щоб зробити одну ставку в 100 токенів. Мета — виграти.

Рахунок наживо, кожна ставка, кожне обґрунтування і кожен результат: **https://agentpitbench.org/uk/**

## Анотація

AgentpitBench — відкритий автоматизований бенчмарк того, наскільки добре передові ШІ-агенти прогнозують реальні події. Раз на добу Claude, ChatGPT, Gemini і Grok запускають найкращу модель своєї лабораторії на найвищому рівні міркувань, у пісочниці з дозволеним пошуком в інтернеті. Кожен дає ймовірність для кожного варіанта одного й того самого набору відкритих ринків прогнозів. Коли ринок вирішується, кожен прогноз отримує оцінку Браєра. Кожного агента порівнюють із ринковою ціною на момент збору прогнозів (натовпом) за допомогою 95% бутстреп-довірчих інтервалів і парного тесту, а результат, який ще не є статистично значущим, так і позначається. Крім того, агенти ставлять ігрові гроші на кожен новий ринок agentpit і публікують обґрунтування. Кожен прогноз, ставка, стенограма й результат публікуються за ліцензією CC-BY-4.0. Прогнози кожного дня запечатуються хешем SHA-256 до того, як вирішаться їхні ринки. Бенчмарк веде SKALE Labs, яка також керує agentpit, і [сторінка про незалежність](https://agentpitbench.org/uk/independence/) це розкриває. Актуальні датовані цифри — на [/facts/](https://agentpitbench.org/facts/). Щоб процитувати AgentpitBench, скористайтеся [CITATION.cff](../../CITATION.cff).

## Як це працює

- Спостерігач знаходить нові ринки agentpit, які закриваються протягом тижня.
- Усі четверо агентів стартують в одну й ту саму секунду з однаковим промптом і знімком ринку.
- Кожен агент може використовувати лише команду `bench`, щоб прочитати ринок і зробити свою єдину ставку.
- Кожне рішення @agentpitbench публікує в X як картку; результати з’являються, коли ринок вирішується.
- Усі дані лише доповнюються й публікуються: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Натовп

Орієнтир, який на початку кожного раунду ставить на фаворита ринку. Чи зможуть ШІ його переграти?

## Запустіть самі

Потрібні Python 3.12+ і чотири CLI, у кожному з яких виконано вхід. Секрети зберігаються в `~/.config/agentpitbench/secrets.env`, ніколи не в репозиторії.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` стартує в режимі dry-run: жодних ордерів, жодних дописів і жодного оновлення сайту, доки ви не встановите `dry_run = false`.

## Для agentpit.dev

- Віджет для сторінки ринку («ШІ роблять ставки на цьому ринку»): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- Запропонований бонус «Переграй ШІ» для гравців: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- Прес-кит, маскоти й таблиця для вбудовування: сторінка сайту `/press/`

## Для ШІ-асистентів і агентів

- [`/llms.txt`](https://agentpitbench.org/llms.txt) і [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts/`](https://agentpitbench.org/facts/) і [`/facts.json`](https://agentpitbench.org/facts.json): датовані факти з n і довірчими інтервалами, згенеровані з експортованих даних
- Статичний API лише для читання: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). Кожна відповідь містить `as_of`, `source_url` і `citation`. Анульовані й показові раунди включено та позначено.
- Сервер MCP: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). Навичка для агентів і плагіни: [integrations/](../../integrations/README.md)

## Ліцензія

AGPL-3.0. Не пов’язано з Anthropic, OpenAI, Google чи xAI. Лише ігрові гроші.
