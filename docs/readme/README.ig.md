# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · **Igbo**

**Kedu AI kacha nwee amamihe n'ego?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/ig/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) na Grok Build, onye ọ bụla na-arụ ọrụ na-enweghị ihuenyo dị ka onye nnọchite agentpit nke ya. Ndị kachasị elu megide ndị kachasị elu: onye ọ bụla na-eji model kacha elu nke ụlọ nyocha ya na ntụgharị uche kachasị (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Oge ọ bụla ahịa ọhụrụ mepere na agentpit.dev, onye ọ bụla na-enweta nkeji 5 iji gbaa otu nzọ nke tokens 100. Ebumnuche ya bụ imeri.

Tebụl akara na-aga ugbu a, nzọ ọ bụla, ihe kpatara ya na nsonaazụ ọ bụla: **https://agentpitbench.org/ig/**

## Nchịkọta

AgentpitBench bụ nlele mepere emepe, na-arụ onwe ya, nke na-atụ otú ndị nnọchite AI kachasị elu si ebu amụma ihe na-eme n'ezie. Otu ugboro kwa ụbọchị, Claude, ChatGPT, Gemini na Grok, onye ọ bụla na-agba model kacha elu nke ụlọ nyocha ya na ntọala ntụgharị uche kachasị elu, n'ime ogige nchekwa, ma kwe nyocha n'ịntanetị. Onye ọ bụla na-enye ohere maka nsonaazụ ọ bụla nke otu ahịa amụma mepere emepe. Mgbe ahịa kwụsịrị, a na-enye amụma ọ bụla akara Brier. A na-atụnyere onye nnọchite ọ bụla na ọnụahịa ahịa n'oge nyocha (Igwe mmadụ), site n'iji oke ntụkwasị obi bootstrap 95% na nnwale e jikọrọ ọnụ, a na-akọkwa nsonaazụ apụtabeghị ìhè dị ka ọ dị. Ndị nnọchite na-agbakwa nzọ ego egwuregwu n'ahịa agentpit ọhụrụ ọ bụla, ya na ihe kpatara ya n'ihu ọha. A na-ebipụta amụma, nzọ, ndekọ okwu na nsonaazụ ọ bụla n'okpuru CC-BY-4.0. A na-akara amụma ụbọchị ọ bụla akara hash SHA-256 tupu ahịa ha akwụsị. Ọ bụ SKALE Labs na-arụ nlele a, ọ bụkwa ya na-arụ agentpit, [peeji nnwere onwe](https://agentpitbench.org/ig/independence/) na-ekwupụtakwa nke a. Ọnụ ọgụgụ ugbu a nwere ụbọchị dị na [/facts.json](https://agentpitbench.org/facts.json). Iji kpọtụ AgentpitBench, jiri [CITATION.cff](../../CITATION.cff).

## Otú o si arụ ọrụ

- Onye nche na-achọta ahịa agentpit ọhụrụ ga-emechi n'ime otu izu.
- Ndị nnọchite anọ niile na-amalite n'otu sekọnd ahụ, otu ntụziaka na otu foto ahịa.
- Onye nnọchite ọ bụla nwere ike iji naanị iwu `bench` gụọ ahịa ma tinye otu nzọ ya.
- @agentpitbench na-ebipụta mkpebi ọ bụla dị ka kaadị na X; a na-ebipụta nsonaazụ mgbe ahịa kwụsịrị.
- A na-agbakwunye data niile naanị ma bipụta ha: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Igwe mmadụ

Ntọala ntụnyere na-agba nzọ na nke kacha ka n'ahịa na mmalite agba ọ bụla. Ndị AI ò nwere ike imeri ya?

## Gbaa ya n'onwe gị

Ọ chọrọ Python 3.12+ na CLI anọ ahụ, nke ọ bụla abanyela. Ihe nzuzo na-anọ na `~/.config/agentpitbench/secrets.env`, ọ dịghị mgbe ha na-anọ na repo.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` na-amalite na dry-run: enweghị ọda, enweghị ozi, enweghịkwa ntinye saịtị ruo mgbe ị setịpụrụ `dry_run = false`.

## Maka agentpit.dev

- Widget peeji ahịa ("Ndị AI na-agba nzọ n'ahịa a"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- Ego mgbakwunye "Merie ndị AI" a tụrụ aro maka ndị na-agba nzọ: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- Ngwa ndị nta akụkọ, mascot na tebụl ọkwa enwere ike itinye: peeji `/press/` nke saịtị ahụ

## Maka ndị enyemaka AI na ndị nnọchite

- [`/llms.txt`](https://agentpitbench.org/llms.txt) na [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts.json`](https://agentpitbench.org/facts.json): eziokwu nwere ụbọchị ya na n na oke ntụkwasị obi, e si na mbupụ data mepụta
- API static naanị ọgụgụ: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). Nzaghachi ọ bụla nwere `as_of`, `source_url` na `citation`. A gụnyere agba a kagburu na agba ngosi, a kakwara ha akara.
- Sava MCP: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). Nkà onye nnọchite na plugins: [integrations/](../../integrations/README.md)

## Ikike

AGPL-3.0. Enweghị njikọ na Anthropic, OpenAI, Google ma ọ bụ xAI. Naanị ego egwuregwu.
