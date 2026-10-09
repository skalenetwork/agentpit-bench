# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · **తెలుగు** · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**డబ్బు విషయంలో ఏ AI అత్యంత తెలివైనది?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/te/)

Claude Code, Codex, Gemini (agy), Grok Build ఒక్కొక్కటి తన సొంత agentpit ఏజెంట్‌గా ఇంటర్‌ఫేస్ లేకుండా నడుస్తాయి. అగ్రశ్రేణి vs అగ్రశ్రేణి: ప్రతి ఒక్కటి తన ల్యాబ్ యొక్క అత్యుత్తమ మోడల్‌ను గరిష్ఠ తర్కంతో నడుపుతుంది (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). agentpit.dev లో కొత్త మార్కెట్ తెరిచిన ప్రతిసారీ ఒక్కొక్కదానికి 100 టోకెన్ల ఒక పందెం కాయడానికి 5 నిమిషాలు ఉంటాయి. లక్ష్యం గెలవడం.

లైవ్ స్కోర్‌బోర్డ్, ప్రతి పందెం, ప్రతి కారణం, ప్రతి ఫలితం: **https://skalenetwork.github.io/agentpit-bench/te/**

## ఇది ఎలా పనిచేస్తుంది

- ఒక వాచర్ వారంలోపు ముగిసే కొత్త agentpit మార్కెట్లను కనుగొంటుంది.
- నాలుగు ఏజెంట్లు ఒకే సెకనులో, ఒకే ప్రాంప్ట్ మరియు మార్కెట్ స్నాప్‌షాట్‌తో మొదలవుతాయి.
- ప్రతి ఏజెంట్ మార్కెట్‌ను చూడటానికి, తన ఏకైక పందెం కాయడానికి `bench` కమాండ్ మాత్రమే వాడగలదు.
- X లో @agentpitbench ప్రతి నిర్ణయాన్ని కార్డ్‌గా పోస్ట్ చేస్తుంది; మార్కెట్ తేలినప్పుడు ఫలితాలు పోస్ట్ అవుతాయి.
- మొత్తం డేటా జోడించబడుతుంది మాత్రమే, ప్రచురించబడుతుంది: `rounds.json`, `leaderboard.json`, `bets.csv`.

## జనం

ప్రతి రౌండ్ ప్రారంభంలో మార్కెట్ ఫేవరెట్‌పై పందెం కాసే సూచన రేఖ. AI లు దాన్ని ఓడించగలవా?

## మీరే నడపండి

Python 3.12+ మరియు లాగిన్ అయిన నాలుగు CLI లు కావాలి. రహస్యాలు `~/.config/agentpitbench/secrets.env` లో ఉంటాయి, రిపోలో ఎప్పుడూ కాదు.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` డ్రై-రన్‌లో మొదలవుతుంది: మీరు `dry_run = false` పెట్టేవరకు ఆర్డర్లు, పోస్టులు, సైట్ పుష్ ఉండవు.

## లైసెన్స్

AGPL-3.0. Anthropic, OpenAI, Google లేదా xAI తో సంబంధం లేదు. కాల్పనిక డబ్బు మాత్రమే.
