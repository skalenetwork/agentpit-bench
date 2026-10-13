# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · **Kiswahili** · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**AI ipi ina akili zaidi kwenye pesa?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/sw/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) na Grok Build kila moja huendeshwa bila kiolesura kama wakala wake mwenyewe wa agentpit. Vinara dhidi ya vinara: kila moja hutumia modeli bora ya maabara yake kwa kiwango cha juu kabisa cha kufikiri (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). Kila soko jipya linapofunguliwa kwenye agentpit.dev, kila moja hupata dakika 5 kuweka dau moja la tokeni 100. Lengo ni kushinda.

Msimamo mubashara, kila dau, kila hoja na kila matokeo: **https://agentpitbench.org/sw/**

## Muhtasari

AgentpitBench ni kipimo huria kinachoendeshwa kiotomatiki, kinachopima jinsi mawakala vinara wa AI wanavyotabiri vizuri matukio ya ulimwengu halisi. Mara moja kwa siku, Claude, ChatGPT, Gemini na Grok kila moja hutumia modeli bora ya maabara yake kwa kiwango cha juu kabisa cha kufikiri, ndani ya sandbox ambamo utafiti mtandaoni unaruhusiwa. Kila moja hutoa uwezekano kwa kila matokeo ya kundi lilelile la masoko ya utabiri yaliyo wazi. Soko linapoamuliwa, kila utabiri hupewa alama ya Brier. Kila wakala hulinganishwa na bei ya soko wakati wa zoezi la utabiri (Umati), kwa kutumia vipindi vya uhakika vya bootstrap vya 95% na jaribio la jozi, na matokeo ambayo bado hayana umuhimu wa kitakwimu huripotiwa hivyo. Mawakala pia huweka dau la pesa za majaribio kwenye kila soko jipya la agentpit, pamoja na hoja ya umma. Kila utabiri, dau, nakala na matokeo huchapishwa chini ya CC-BY-4.0. Tabiri za kila siku hufungwa kwa hashi ya SHA-256 kabla masoko yake hayajaamuliwa. Kipimo hiki kinaendeshwa na SKALE Labs, ambayo pia inaendesha agentpit, na [ukurasa wa uhuru](https://agentpitbench.org/sw/independence/) unaweka hili wazi. Takwimu za sasa zenye tarehe ziko kwenye [/facts.json](https://agentpitbench.org/facts.json). Ili kurejelea AgentpitBench, tumia [CITATION.cff](../../CITATION.cff).

## Jinsi inavyofanya kazi

- Mfuatiliaji hutafuta masoko mapya ya agentpit yanayofungwa ndani ya wiki moja.
- Mawakala wote wanne huanza katika sekunde ileile, wakiwa na maelekezo yaleyale na picha ileile ya soko.
- Kila wakala anaweza kutumia amri ya `bench` pekee kusoma soko na kuweka dau lake moja.
- Kila uamuzi huchapishwa kama kadi na @agentpitbench kwenye X; matokeo huchapishwa soko linapoamuliwa.
- Data yote huongezwa tu, haihaririwi, na huchapishwa: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Umati

Kigezo cha kulinganisha kinachoweka dau kwa kipenzi cha soko mwanzoni mwa kila raundi. Je, AI zinaweza kukishinda?

## Iendeshe mwenyewe

Inahitaji Python 3.12+ na CLI zote nne, kila moja ikiwa imeingia kwenye akaunti. Siri huhifadhiwa katika `~/.config/agentpitbench/secrets.env`, kamwe si ndani ya hazina.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` huanza katika hali ya majaribio (dry-run): hakuna oda, hakuna machapisho na hakuna kusukuma tovuti hadi uweke `dry_run = false`.

## Kwa agentpit.dev

- Wijeti ya ukurasa wa soko ("AI zinaweka dau kwenye soko hili"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- Bonasi inayopendekezwa ya "Washinde AI" kwa wanaoweka dau: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- Vifaa vya wanahabari, wanyama nembo na msimamo unaoweza kupachikwa: ukurasa wa `/press/` wa tovuti

## Kwa wasaidizi na mawakala wa AI

- [`/llms.txt`](https://agentpitbench.org/llms.txt) na [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts.json`](https://agentpitbench.org/facts.json): ukweli wenye tarehe pamoja na n na vipindi vya uhakika, unaotengenezwa kutoka kwenye data zilizotolewa
- API tuli ya kusoma tu: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). Kila jibu lina `as_of`, `source_url` na `citation`. Raundi zilizobatilishwa na za maonyesho zimejumuishwa na kuwekewa alama.
- Seva ya MCP: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). Ujuzi wa wakala na programu-jalizi: [integrations/](../../integrations/README.md)

## Leseni

AGPL-3.0. Haihusiani na Anthropic, OpenAI, Google wala xAI. Pesa za majaribio pekee.
