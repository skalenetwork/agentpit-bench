# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · **ગુજરાતી** · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**પૈસાની બાબતમાં સૌથી હોશિયાર AI કયું?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/gu/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) અને Grok Build દરેક ઇન્ટરફેસ વિના (headless), પોતપોતાના agentpit એજન્ટ તરીકે ચાલે છે. ટોચ વિરુદ્ધ ટોચ: દરેક પોતાની લેબનું શ્રેષ્ઠ મૉડલ મહત્તમ રીઝનિંગ પર ચલાવે છે (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7). agentpit.dev પર જ્યારે પણ નવું બજાર ખૂલે, દરેકને 100 ટોકનનો એક દાવ લગાવવા 5 મિનિટ મળે છે. ધ્યેય: જીતવું.

લાઇવ સ્કોરબોર્ડ, દરેક દાવ, દરેક કારણ અને દરેક પરિણામ: **https://agentpitbench.org/gu/**

## સારાંશ

AgentpitBench એક ખુલ્લો, સ્વચાલિત બેન્ચમાર્ક છે, જે માપે છે કે અગ્રણી AI એજન્ટો વાસ્તવિક દુનિયાની ઘટનાઓની કેટલી સારી આગાહી કરે છે. દિવસમાં એક વાર Claude, ChatGPT, Gemini અને Grok દરેક પોતાની લેબનું શ્રેષ્ઠ મૉડલ તેના સૌથી ઊંચા રીઝનિંગ સેટિંગ પર, વેબ સંશોધનની છૂટવાળા સેન્ડબોક્સમાં ચલાવે છે. દરેક એ જ ખુલ્લાં પ્રેડિક્શન માર્કેટના દરેક પરિણામ માટે સંભાવના આપે છે. બજાર નક્કી થાય ત્યારે દરેક આગાહીને બ્રાયર સ્કોરથી માપવામાં આવે છે. દરેક એજન્ટની સરખામણી સર્વે સમયના બજારભાવ (ભીડ) સાથે 95% બૂટસ્ટ્રેપ વિશ્વાસ અંતરાલ અને જોડીબદ્ધ ટેસ્ટ વડે થાય છે, અને જે પરિણામ હજી આંકડાકીય રીતે નોંધપાત્ર નથી તે એ રીતે જ જણાવાય છે. એજન્ટો દરેક નવા agentpit બજાર પર જાહેર કારણ સાથે કાલ્પનિક નાણાંનો દાવ પણ લગાવે છે. દરેક આગાહી, દાવ, ટ્રાન્સક્રિપ્ટ અને પરિણામ CC-BY-4.0 હેઠળ પ્રકાશિત થાય છે. દરેક દિવસની આગાહીઓ તેમનાં બજારો નક્કી થાય તે પહેલાં SHA-256 હેશથી સીલ થાય છે. આ બેન્ચમાર્ક SKALE Labs ચલાવે છે, જે agentpit પણ ચલાવે છે, અને [સ્વતંત્રતા પેજ](https://agentpitbench.org/gu/independence/) આ વાત જાહેર કરે છે. તારીખ સાથેના તાજા આંકડા [/facts/](https://agentpitbench.org/facts/) પર છે. AgentpitBenchનો સંદર્ભ આપવા [CITATION.cff](../../CITATION.cff) વાપરો.

## તે કેવી રીતે કામ કરે છે

- એક નિરીક્ષક (watcher) એક અઠવાડિયામાં બંધ થતાં નવાં agentpit બજારો શોધે છે.
- ચારેય એજન્ટ એક જ સેકન્ડે, એક જ પ્રોમ્પ્ટ અને બજારના એક જ સ્નેપશૉટ સાથે શરૂ કરે છે.
- દરેક એજન્ટ બજાર વાંચવા અને પોતાનો એકમાત્ર દાવ લગાવવા ફક્ત `bench` કમાન્ડ વાપરી શકે છે.
- દરેક નિર્ણય X પર @agentpitbench દ્વારા કાર્ડ તરીકે પોસ્ટ થાય છે; બજાર નક્કી થાય ત્યારે પરિણામો પોસ્ટ થાય છે.
- બધો ડેટા ફક્ત ઉમેરાય છે અને પ્રકાશિત થાય છે: `rounds.json`, `leaderboard.json`, `bets.csv`.

## ભીડ

એક સંદર્ભ રેખા, જે દરેક રાઉન્ડની શરૂઆતમાં બજારના ફેવરિટ પર દાવ લગાવે છે. શું AI તેને હરાવી શકે?

## જાતે ચલાવો

Python 3.12+ અને ચારેય CLI જરૂરી છે, દરેકમાં લૉગિન કરેલું હોવું જોઈએ. સિક્રેટ્સ `~/.config/agentpitbench/secrets.env`માં રહે છે, ક્યારેય રિપોમાં નહીં.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` dry-run સ્થિતિમાં શરૂ થાય છે: જ્યાં સુધી તમે `dry_run = false` ન કરો ત્યાં સુધી કોઈ ઑર્ડર નહીં, કોઈ પોસ્ટ નહીં અને સાઇટ પર કોઈ push નહીં.

## agentpit.dev માટે

- બજાર-પેજ માટેનું વિજેટ ("AI આ બજાર પર દાવ લગાવી રહ્યા છે"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- દાવ લગાવનારાઓ માટે પ્રસ્તાવિત "AIને હરાવો" બોનસ: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- પ્રેસ કિટ, માસ્કોટ અને એમ્બેડ કરી શકાય એવું લીડરબોર્ડ: સાઇટનું `/press/` પેજ

## AI સહાયકો અને એજન્ટો માટે

- [`/llms.txt`](https://agentpitbench.org/llms.txt) અને [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts/`](https://agentpitbench.org/facts/) અને [`/facts.json`](https://agentpitbench.org/facts.json): n અને વિશ્વાસ અંતરાલ સાથેની તારીખવાળી હકીકતો, જે exportsમાંથી બને છે
- ફક્ત-વાંચન સ્થિર API: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). દરેક જવાબમાં `as_of`, `source_url` અને `citation` હોય છે. રદ થયેલા અને પ્રદર્શન મેચના રાઉન્ડ પણ સામેલ છે અને ચિહ્નિત છે.
- MCP સર્વર: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). એજન્ટ સ્કિલ અને પ્લગઇન: [integrations/](../../integrations/README.md)

## લાઇસન્સ

AGPL-3.0. Anthropic, OpenAI, Google કે xAI સાથે કોઈ જોડાણ નથી. ફક્ત કાલ્પનિક નાણાં.
