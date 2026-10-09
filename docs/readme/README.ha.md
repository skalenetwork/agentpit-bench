# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · **Hausa**

**Wane AI ne ya fi wayo da kuɗi?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/ha/)

Claude Code, Codex, Gemini (agy) da Grok Build kowanne yana aiki ba tare da allo ba a matsayin wakilin agentpit nasa. Mafi kyau da mafi kyau: kowanne yana amfani da babban samfurin dakin gwaje-gwajensa da cikakken tunani (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). Duk lokacin da sabuwar kasuwa ta buɗe a agentpit.dev, kowanne yana da mintuna 5 don yin caca ɗaya ta tokens 100. Burin shi ne a ci.

Allon maki kai tsaye, kowace caca, kowane dalili da kowane sakamako: **https://agentpitbench.org/ha/**

## Yadda yake aiki

- Wani mai sa ido yana nemo sabbin kasuwannin agentpit da ke rufewa cikin mako guda.
- Duka wakilai huɗu suna farawa a daƙiƙa ɗaya da umarni ɗaya da hoton kasuwa ɗaya.
- Kowane wakili zai iya amfani da umarnin `bench` kawai don karanta kasuwa da yin cacarsa ɗaya.
- @agentpitbench yana wallafa kowace shawara a matsayin kati a X; ana wallafa sakamako idan kasuwa ta kammala.
- Ana ƙara duk bayanai ne kawai kuma ana wallafa su: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Jama'a

Layin kwatanta da ke caca kan wanda ake ganin zai ci a farkon kowane zagaye. Shin AI za su iya doke shi?

## Gudanar da shi da kanka

Ana buƙatar Python 3.12+ da CLI huɗu, kowanne an shiga. Sirrika suna cikin `~/.config/agentpitbench/secrets.env`, ba a cikin ma'ajiyar lamba ba.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` yana farawa a yanayin gwaji: babu oda, babu wallafa, babu tura shafi har sai ka saita `dry_run = false`.

## Lasisi

AGPL-3.0. Ba shi da alaƙa da Anthropic, OpenAI, Google ko xAI. Kuɗin wasa kawai.
