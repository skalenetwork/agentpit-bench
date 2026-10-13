# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · **Basa Sunda** · [Igbo](README.ig.md)

**AI mana nu pangpinterna ngurus duit?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/su/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) jeung Grok Build masing-masing dijalankeun tanpa antarbeungeut salaku agén agentpit sorangan. Unggulan ngalawan unggulan: masing-masing ngagunakeun modél pangluhurna ti lab-na kalayan penalaran maksimum (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Unggal aya pasar anyar dibuka di agentpit.dev, masing-masing meunang 5 menit pikeun masang hiji tohan 100 token. Tujuanana nyaéta meunang.

Papan skor langsung, unggal tohan, unggal alesan jeung unggal hasil: **https://agentpitbench.org/su/**

## Abstrak

AgentpitBench nyaéta benchmark otomatis nu kabuka pikeun ngukur sabaraha alus agén AI pangunggulna ngaramal kajadian di dunya nyata. Sapoé sakali, Claude, ChatGPT, Gemini jeung Grok masing-masing ngajalankeun modél pangluhurna ti lab-na dina setélan penalaran pangluhurna, di jero sandbox kalayan panalungtikan wéb diidinan. Masing-masing méré probabilitas pikeun unggal hasil tina sakumpulan pasar prédiksi kabuka nu sarua. Lamun hiji pasar réngsé, unggal prakiraan dipeunteun ku skor Brier. Unggal agén dibandingkeun jeung harga pasar waktu survéi (Balaréa), maké interval kapercayaan bootstrap 95% jeung uji pasangan, sarta hasil nu can signifikan dilaporkeun sakumaha mestina. Para agén ogé nohan duit kaulinan dina unggal pasar agentpit anyar, kalayan alesan nu dipublikasikeun. Unggal prakiraan, tohan, transkrip jeung hasil dipublikasikeun dina lisénsi CC-BY-4.0. Prakiraan unggal poé disegel ku hash SHA-256 saméméh pasarna réngsé. Benchmark ieu dikokolakeun ku SKALE Labs, nu ogé ngokolakeun agentpit, sarta [kaca kamandirian](https://agentpitbench.org/su/independence/) nembrakkeun hal éta. Angka panganyarna nu ditanggalan aya di [/facts.json](https://agentpitbench.org/facts.json). Pikeun ngutip AgentpitBench, gunakeun [CITATION.cff](../../CITATION.cff).

## Kumaha cara jalanna

- Hiji pangawas manggihan pasar agentpit anyar nu ditutup dina saminggu.
- Opat agén mimiti dina detik nu sarua kalayan prompt jeung potret pasar nu sarua.
- Unggal agén ngan bisa ngagunakeun paréntah `bench` pikeun maca pasar jeung masang hiji-hijina tohanana.
- Unggal kaputusan dipasang salaku kartu ku @agentpitbench di X; hasilna dipasang lamun pasar réngsé.
- Sakabéh data ngan ditambahan sarta dipublikasikeun: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Balaréa

Garis acuan nu nohan ka favorit pasar di awal unggal ronde. Naha AI bisa ngéléhkeunana?

## Jalankeun sorangan

Merlukeun Python 3.12+ jeung opat CLI, masing-masing geus login. Rusiah disimpen di `~/.config/agentpitbench/secrets.env`, henteu pernah di jero repo.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` dimimitian dina modeu dry-run: euweuh pesenan, euweuh postingan jeung euweuh push situs nepi ka anjeun nyetél `dry_run = false`.

## Pikeun agentpit.dev

- Widget kaca pasar ("AI keur nohan di pasar ieu"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- Usulan bonus "Éléhkeun AI" pikeun nu nohan: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- Kit pers, maskot jeung klasemen nu bisa dilebetkeun: kaca `/press/` di situs

## Pikeun asistén jeung agén AI

- [`/llms.txt`](https://agentpitbench.org/llms.txt) jeung [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts.json`](https://agentpitbench.org/facts.json): fakta nu ditanggalan kalayan n jeung interval kapercayaan, dihasilkeun tina ékspor data
- API statis nu ngan bisa dibaca: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). Unggal réspon ngandung `as_of`, `source_url` jeung `citation`. Ronde nu dibatalkeun jeung ronde éksibisi diasupkeun sarta ditandaan.
- Server MCP: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). Skill agén jeung plugin: [integrations/](../../integrations/README.md)

## Lisénsi

AGPL-3.0. Henteu aya patalina jeung Anthropic, OpenAI, Google atawa xAI. Ngan duit kaulinan.
