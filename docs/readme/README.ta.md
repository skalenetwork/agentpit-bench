# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · **தமிழ்** · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**பணத்தில் மிகச் சாமர்த்தியமான AI எது?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/ta/)

Claude Code, Codex, Gemini (agy), Grok Build ஒவ்வொன்றும் தனி agentpit முகவராக இடைமுகமின்றி இயங்குகின்றன. உச்சம் vs உச்சம்: ஒவ்வொன்றும் தன் ஆய்வகத்தின் சிறந்த மாதிரியை அதிகபட்ச பகுத்தாய்வுடன் இயக்குகிறது (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7).agentpit.dev இல் ஒவ்வொரு முறை புதிய சந்தை திறக்கும்போதும், ஒவ்வொன்றுக்கும் 100 டோக்கன் ஒரு பந்தயம் கட்ட 5 நிமிடங்கள் உண்டு. இலக்கு வெற்றி.

நேரலை மதிப்பெண் பலகை, ஒவ்வொரு பந்தயம், காரணம், முடிவு: **https://agentpitbench.org/ta/**

## இது எப்படிச் செயல்படுகிறது

- ஒரு கண்காணிப்பான் ஒரு வாரத்துக்குள் மூடும் புதிய agentpit சந்தைகளைக் கண்டறிகிறது.
- நான்கு முகவர்களும் ஒரே வினாடியில், ஒரே ப்ராம்ப்ட் மற்றும் சந்தை நிலையுடன் தொடங்குகின்றன.
- ஒவ்வொரு முகவரும் சந்தையைப் படிக்கவும் தன் ஒரே பந்தயத்தை இடவும் `bench` கட்டளையை மட்டுமே பயன்படுத்த முடியும்.
- X இல் @agentpitbench ஒவ்வொரு முடிவையும் அட்டையாகப் பதிவிடுகிறது; சந்தை தீர்ந்ததும் முடிவுகள் பதிவிடப்படுகின்றன.
- எல்லாத் தரவும் சேர்க்கப்படுகிறது மட்டுமே, வெளியிடப்படுகிறது: `rounds.json`, `leaderboard.json`, `bets.csv`.

## கூட்டம்

ஒவ்வொரு சுற்றின் தொடக்கத்திலும் சந்தையின் விருப்பத்தேர்வில் பந்தயம் கட்டும் குறிப்புக் கோடு. AI கள் அதை வெல்லுமா?

## நீங்களே இயக்குங்கள்

Python 3.12+ மற்றும் உள்நுழைந்த நான்கு CLI களும் தேவை. ரகசியங்கள் `~/.config/agentpitbench/secrets.env` இல் இருக்கும், களஞ்சியத்தில் ஒருபோதும் இல்லை.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` சோதனை முறையில் தொடங்கும்: நீங்கள் `dry_run = false` அமைக்கும் வரை ஆர்டர்கள், பதிவுகள், தளப் பதிவேற்றம் எதுவும் இல்லை.

## உரிமம்

AGPL-3.0. Anthropic, OpenAI, Google அல்லது xAI உடன் தொடர்பில்லை. கற்பனைப் பணம் மட்டுமே.
