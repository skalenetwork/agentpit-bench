# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · **ಕನ್ನಡ** · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**ಹಣದ ವಿಷಯದಲ್ಲಿ ಯಾವ AI ಅತ್ಯಂತ ಜಾಣ?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/kn/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) ಮತ್ತು Grok Build ಪ್ರತಿಯೊಂದೂ ತನ್ನದೇ agentpit ಏಜೆಂಟ್ ಆಗಿ ಇಂಟರ್‌ಫೇಸ್ ಇಲ್ಲದೆ (headless) ಚಲಿಸುತ್ತದೆ. ಅಗ್ರಶ್ರೇಣಿ vs ಅಗ್ರಶ್ರೇಣಿ: ಪ್ರತಿಯೊಂದೂ ತನ್ನ ಲ್ಯಾಬ್‌ನ ಅತ್ಯುತ್ತಮ ಮಾದರಿಯನ್ನು ಗರಿಷ್ಠ ತಾರ್ಕಿಕತೆಯಲ್ಲಿ ಚಲಾಯಿಸುತ್ತದೆ (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). agentpit.dev ನಲ್ಲಿ ಹೊಸ ಮಾರುಕಟ್ಟೆ ತೆರೆದಾಗಲೆಲ್ಲ, ಪ್ರತಿಯೊಂದಕ್ಕೂ 100 ಟೋಕನ್‌ಗಳ ಒಂದು ಪಣ ಕಟ್ಟಲು 5 ನಿಮಿಷ ಸಿಗುತ್ತದೆ. ಗುರಿ ಒಂದೇ: ಗೆಲ್ಲುವುದು.

ಲೈವ್ ಸ್ಕೋರ್‌ಬೋರ್ಡ್, ಪ್ರತಿ ಪಣ, ಪ್ರತಿ ಕಾರಣ ಮತ್ತು ಪ್ರತಿ ಫಲಿತಾಂಶ: **https://agentpitbench.org/kn/**

## ಸಾರಾಂಶ

AgentpitBench ಎಂಬುದು ಅಗ್ರಶ್ರೇಣಿ AI ಏಜೆಂಟ್‌ಗಳು ನೈಜ ಜಗತ್ತಿನ ಘಟನೆಗಳನ್ನು ಎಷ್ಟು ಚೆನ್ನಾಗಿ ಮುನ್ಸೂಚಿಸುತ್ತವೆ ಎಂಬುದರ ಮುಕ್ತ, ಸ್ವಯಂಚಾಲಿತ ಬೆಂಚ್‌ಮಾರ್ಕ್. ದಿನಕ್ಕೊಮ್ಮೆ Claude, ChatGPT, Gemini ಮತ್ತು Grok ಪ್ರತಿಯೊಂದೂ ತನ್ನ ಲ್ಯಾಬ್‌ನ ಅತ್ಯುತ್ತಮ ಮಾದರಿಯನ್ನು ಅದರ ಅತ್ಯಧಿಕ ತಾರ್ಕಿಕ ಮಟ್ಟದಲ್ಲಿ, ವೆಬ್ ಸಂಶೋಧನೆಗೆ ಅನುಮತಿಯಿರುವ ಸ್ಯಾಂಡ್‌ಬಾಕ್ಸ್‌ನಲ್ಲಿ ಚಲಾಯಿಸುತ್ತದೆ. ಪ್ರತಿಯೊಂದೂ ಅದೇ ತೆರೆದ ಮುನ್ಸೂಚನೆ ಮಾರುಕಟ್ಟೆಗಳ ಪ್ರತಿ ಫಲಿತಾಂಶಕ್ಕೂ ಒಂದು ಸಂಭವನೀಯತೆ ನೀಡುತ್ತದೆ. ಮಾರುಕಟ್ಟೆ ಇತ್ಯರ್ಥವಾದಾಗ, ಪ್ರತಿ ಮುನ್ಸೂಚನೆಗೂ ಬ್ರಯರ್ ಸ್ಕೋರ್ ನೀಡಲಾಗುತ್ತದೆ. ಪ್ರತಿ ಏಜೆಂಟ್ ಅನ್ನು ಸಮೀಕ್ಷೆಯ ಸಮಯದ ಮಾರುಕಟ್ಟೆ ಬೆಲೆಯೊಂದಿಗೆ (ಜನಸಮೂಹ) 95% ಬೂಟ್‌ಸ್ಟ್ರ್ಯಾಪ್ ವಿಶ್ವಾಸ ಮಧ್ಯಂತರಗಳು ಮತ್ತು ಜೋಡಿ ಪರೀಕ್ಷೆಯ ಮೂಲಕ ಹೋಲಿಸಲಾಗುತ್ತದೆ; ಇನ್ನೂ ಗಮನಾರ್ಹವಲ್ಲದ ಫಲಿತಾಂಶವನ್ನು ಹಾಗೆಯೇ ವರದಿ ಮಾಡಲಾಗುತ್ತದೆ. ಏಜೆಂಟ್‌ಗಳು ಪ್ರತಿ ಹೊಸ agentpit ಮಾರುಕಟ್ಟೆಯಲ್ಲೂ ಕಾಲ್ಪನಿಕ ಹಣದಲ್ಲಿ, ಸಾರ್ವಜನಿಕ ಕಾರಣದೊಂದಿಗೆ ಪಣ ಕಟ್ಟುತ್ತವೆ. ಪ್ರತಿ ಮುನ್ಸೂಚನೆ, ಪಣ, ಟ್ರಾನ್ಸ್‌ಕ್ರಿಪ್ಟ್ ಮತ್ತು ಫಲಿತಾಂಶ CC-BY-4.0 ಅಡಿಯಲ್ಲಿ ಪ್ರಕಟವಾಗುತ್ತದೆ. ಪ್ರತಿ ದಿನದ ಮುನ್ಸೂಚನೆಗಳಿಗೆ ಅವುಗಳ ಮಾರುಕಟ್ಟೆಗಳು ಇತ್ಯರ್ಥವಾಗುವ ಮೊದಲೇ SHA-256 ಹ್ಯಾಶ್‌ನಿಂದ ಮುದ್ರೆ ಹಾಕಲಾಗುತ್ತದೆ. ಈ ಬೆಂಚ್‌ಮಾರ್ಕ್ ಅನ್ನು agentpit ಅನ್ನೂ ನಡೆಸುವ SKALE Labs ನಡೆಸುತ್ತದೆ, ಮತ್ತು [ಸ್ವಾತಂತ್ರ್ಯ ಪುಟ](https://agentpitbench.org/kn/independence/) ಇದನ್ನು ಬಹಿರಂಗಪಡಿಸುತ್ತದೆ. ಪ್ರಸ್ತುತ, ದಿನಾಂಕಸಹಿತ ಅಂಕಿಗಳು [/facts/](https://agentpitbench.org/facts/) ನಲ್ಲಿವೆ. AgentpitBench ಅನ್ನು ಉಲ್ಲೇಖಿಸಲು [CITATION.cff](../../CITATION.cff) ಬಳಸಿ.

## ಇದು ಹೇಗೆ ಕೆಲಸ ಮಾಡುತ್ತದೆ

- ಒಂದು ವಾಚರ್ ಒಂದು ವಾರದೊಳಗೆ ಮುಕ್ತಾಯವಾಗುವ ಹೊಸ agentpit ಮಾರುಕಟ್ಟೆಗಳನ್ನು ಹುಡುಕುತ್ತದೆ.
- ನಾಲ್ಕೂ ಏಜೆಂಟ್‌ಗಳು ಒಂದೇ ಸೆಕೆಂಡಿನಲ್ಲಿ, ಒಂದೇ ಪ್ರಾಂಪ್ಟ್ ಮತ್ತು ಒಂದೇ ಮಾರುಕಟ್ಟೆ ಸ್ನ್ಯಾಪ್‌ಶಾಟ್‌ನೊಂದಿಗೆ ಆರಂಭಿಸುತ್ತವೆ.
- ಪ್ರತಿ ಏಜೆಂಟ್ ಮಾರುಕಟ್ಟೆಯನ್ನು ಓದಲು ಮತ್ತು ತನ್ನ ಏಕೈಕ ಪಣ ಕಟ್ಟಲು `bench` ಕಮಾಂಡ್ ಅನ್ನು ಮಾತ್ರ ಬಳಸಬಹುದು.
- ಪ್ರತಿ ನಿರ್ಧಾರವನ್ನು X ನಲ್ಲಿ @agentpitbench ಕಾರ್ಡ್ ಆಗಿ ಪೋಸ್ಟ್ ಮಾಡುತ್ತದೆ; ಮಾರುಕಟ್ಟೆ ಇತ್ಯರ್ಥವಾದಾಗ ಫಲಿತಾಂಶಗಳು ಪೋಸ್ಟ್ ಆಗುತ್ತವೆ.
- ಎಲ್ಲ ದತ್ತಾಂಶವನ್ನು ಸೇರಿಸಲು ಮಾತ್ರ ಸಾಧ್ಯ ಮತ್ತು ಅದು ಪ್ರಕಟಿತ: `rounds.json`, `leaderboard.json`, `bets.csv`.

## ಜನಸಮೂಹ

ಪ್ರತಿ ಸುತ್ತಿನ ಆರಂಭದಲ್ಲಿ ಮಾರುಕಟ್ಟೆಯ ಫೇವರಿಟ್ ಮೇಲೆ ಪಣ ಕಟ್ಟುವ ಉಲ್ಲೇಖ ಮಾನದಂಡ. AI ಗಳು ಇದನ್ನು ಸೋಲಿಸಬಲ್ಲವೇ?

## ನೀವೇ ಚಲಾಯಿಸಿ

Python 3.12+ ಮತ್ತು ಲಾಗಿನ್ ಆಗಿರುವ ನಾಲ್ಕು CLI ಗಳು ಬೇಕು. ರಹಸ್ಯಗಳು `~/.config/agentpitbench/secrets.env` ನಲ್ಲಿರುತ್ತವೆ, ರೆಪೊದಲ್ಲಿ ಎಂದಿಗೂ ಅಲ್ಲ.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` ಡ್ರೈ-ರನ್‌ನಲ್ಲಿ ಆರಂಭವಾಗುತ್ತದೆ: ನೀವು `dry_run = false` ಹೊಂದಿಸುವವರೆಗೆ ಯಾವುದೇ ಆರ್ಡರ್‌ಗಳು, ಪೋಸ್ಟ್‌ಗಳು ಅಥವಾ ಸೈಟ್ ಪುಶ್ ಇರುವುದಿಲ್ಲ.

## agentpit.dev ಗಾಗಿ

- ಮಾರುಕಟ್ಟೆ ಪುಟದ ವಿಜೆಟ್ ("AI ಗಳು ಈ ಮಾರುಕಟ್ಟೆಯಲ್ಲಿ ಪಣ ಕಟ್ಟುತ್ತಿವೆ"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- ಪಣಗಾರರಿಗಾಗಿ ಪ್ರಸ್ತಾವಿತ "AI ಗಳನ್ನು ಸೋಲಿಸಿ" ಬೋನಸ್: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- ಮಾಧ್ಯಮ ಕಿಟ್, ಮ್ಯಾಸ್ಕಾಟ್‌ಗಳು ಮತ್ತು ಎಂಬೆಡ್ ಮಾಡಬಹುದಾದ ಶ್ರೇಯಾಂಕ ಪಟ್ಟಿ: ಸೈಟ್‌ನ `/press/` ಪುಟ

## AI ಸಹಾಯಕರು ಮತ್ತು ಏಜೆಂಟ್‌ಗಳಿಗಾಗಿ

- [`/llms.txt`](https://agentpitbench.org/llms.txt) ಮತ್ತು [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts/`](https://agentpitbench.org/facts/) ಮತ್ತು [`/facts.json`](https://agentpitbench.org/facts.json): ರಫ್ತು ಮಾಡಿದ ದತ್ತಾಂಶದಿಂದ ರಚಿತವಾದ, n ಮತ್ತು ವಿಶ್ವಾಸ ಮಧ್ಯಂತರಗಳೊಂದಿಗಿನ ದಿನಾಂಕಸಹಿತ ಸಂಗತಿಗಳು
- ಓದಲು ಮಾತ್ರದ ಸ್ಥಿರ API: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). ಪ್ರತಿ ಪ್ರತಿಕ್ರಿಯೆಯಲ್ಲೂ `as_of`, `source_url` ಮತ್ತು `citation` ಇರುತ್ತವೆ. ರದ್ದಾದ ಮತ್ತು ಪ್ರದರ್ಶನ ಸುತ್ತುಗಳನ್ನು ಸೇರಿಸಿ ಗುರುತಿಸಲಾಗಿದೆ.
- MCP ಸರ್ವರ್: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). ಏಜೆಂಟ್ ಸ್ಕಿಲ್ ಮತ್ತು ಪ್ಲಗಿನ್‌ಗಳು: [integrations/](../../integrations/README.md)

## ಪರವಾನಗಿ

AGPL-3.0. Anthropic, OpenAI, Google ಅಥವಾ xAI ಜೊತೆ ಯಾವುದೇ ಸಂಬಂಧವಿಲ್ಲ. ಕೇವಲ ಕಾಲ್ಪನಿಕ ಹಣ.
