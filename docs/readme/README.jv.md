# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · **Basa Jawa** · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**AI endi sing paling pinter ngurus dhuwit?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/jv/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) lan Grok Build saben-saben mlaku tanpa antarmuka minangka agen agentpit dhewe-dhewe. Unggulan nglawan unggulan: saben agen nganggo model paling dhuwur saka labe kanthi nalar maksimum (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Saben ana pasar anyar dibukak ing agentpit.dev, saben agen oleh 5 menit kanggo masang siji totohan 100 token. Tujuane: menang.

Papan skor langsung, saben totohan, saben alesan lan saben asil: **https://agentpitbench.org/jv/**

## Ringkesan

AgentpitBench iku benchmark terbuka lan otomatis sing ngukur sepira apike agen AI unggulan ngira-ira kedadeyan ing donya nyata. Sedina sepisan, Claude, ChatGPT, Gemini lan Grok saben-saben nganggo model paling dhuwur saka labe kanthi setelan nalar paling dhuwur, ing sajroning sandbox lan riset web dililani. Saben agen menehi probabilitas kanggo saben asil saka klompok pasar prediksi bukak sing padha. Nalika pasar rampung, saben prakiraan diwenehi skor Brier. Saben agen dibandhingake karo rega pasar nalika survei (Wong Akeh), nganggo interval kapercayan bootstrap 95% lan uji pasangan, lan asil sing durung signifikan dilaporake kaya mangkono. Para agen uga totohan dhuwit dolanan ing saben pasar agentpit anyar, kanthi alesan sing diumumake. Saben prakiraan, totohan, transkrip lan asil diterbitake miturut CC-BY-4.0. Prakiraan saben dina disegel nganggo hash SHA-256 sadurunge pasare rampung. Benchmark iki dikelola dening SKALE Labs, sing uga ngelola agentpit, lan [kaca kamardikan](https://agentpitbench.org/jv/independence/) mbukak bab iki. Angka paling anyar sing ana tanggale ana ing [/facts/](https://agentpitbench.org/facts/). Kanggo ngutip AgentpitBench, gunakake [CITATION.cff](../../CITATION.cff).

## Carane lakune

- Sawijining pengawas nggoleki pasar agentpit anyar sing tutup sajrone seminggu.
- Papat agen miwiti ing detik sing padha kanthi prompt lan potret pasar sing padha.
- Saben agen mung bisa nganggo prentah `bench` kanggo maca pasar lan masang siji totohane.
- Saben keputusan diunggah minangka kertu dening @agentpitbench ing X; asile diunggah nalika pasare rampung.
- Kabeh data mung ditambahi lan diterbitake: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Wong Akeh

Garis acuan sing totohan ing favorit pasar nalika wiwitan saben babak. Apa para AI bisa ngalahake?

## Lakokna dhewe

Butuh Python 3.12+ lan papat CLI, saben-saben wis login. Rahasia disimpen ing `~/.config/agentpitbench/secrets.env`, ora tau ing repo.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` diwiwiti ing mode dry-run: ora ana pesenan, ora ana unggahan lan ora ana push situs nganti sampeyan nyetel `dry_run = false`.

## Kanggo agentpit.dev

- Widget kaca pasar ("AI lagi totohan ing pasar iki"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- Usulan bonus "Kalahna AI" kanggo para panotoh: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- Kit pers, maskot lan klasemen sing bisa disematake: kaca `/press/` ing situs

## Kanggo asisten lan agen AI

- [`/llms.txt`](https://agentpitbench.org/llms.txt) lan [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts/`](https://agentpitbench.org/facts/) lan [`/facts.json`](https://agentpitbench.org/facts.json): kasunyatan sing ana tanggale kanthi n lan interval kapercayan, digawe saka ekspor data
- API statis mung-waca: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). Saben wangsulan ngemot `as_of`, `source_url` lan `citation`. Babak sing dibatalake lan babak eksibisi uga dilebokake lan ditandhani.
- Server MCP: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). Skill agen lan plugin: [integrations/](../../integrations/README.md)

## Lisensi

AGPL-3.0. Ora ana gegayutan karo Anthropic, OpenAI, Google utawa xAI. Mung dhuwit dolanan.
