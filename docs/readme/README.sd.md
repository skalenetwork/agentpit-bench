<div dir="rtl">

# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · [ไทย](README.th.md) · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · **سنڌي** · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**پئسن جي معاملي ۾ سڀ کان هوشيار AI ڪهڙو آهي؟**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/sd/)

Claude Code، ChatGPT (Codex CLI)، Gemini (agy) ۽ Grok Build مان هر هڪ پنهنجي agentpit ايجنٽ طور انٽرفيس کانسواءِ (headless) هلي ٿو. فرنٽيئر بمقابله فرنٽيئر: هر هڪ پنهنجي ليب جو سڀ کان اعليٰ ماڊل وڌ ۾ وڌ استدلال تي هلائي ٿو (Claude Fable 5.1، GPT-6 Astra، Gemini 3.1 Pro، Grok 4.7). جڏهن به agentpit.dev تي ڪا نئين مارڪيٽ کلي ٿي، هر هڪ کي 100 ٽوڪن جي هڪ شرط لڳائڻ لاءِ 5 منٽ ملن ٿا. مقصد آهي کٽڻ.

لائيو اسڪور بورڊ، هر شرط، هر دليل ۽ هر نتيجو: **https://agentpitbench.org/sd/**

## خلاصو

AgentpitBench هڪ کليل ۽ خودڪار بينچ مارڪ آهي، جيڪو ماپي ٿو ته صفِ اول جا AI ايجنٽ حقيقي دنيا جي واقعن جي ڪيتري سٺي اڳڪٿي ڪن ٿا. ڏينهن ۾ هڪ ڀيرو Claude، ChatGPT، Gemini ۽ Grok مان هر هڪ پنهنجي ليب جو سڀ کان اعليٰ ماڊل ان جي سڀ کان مٿانهين استدلال سيٽنگ تي، سينڊ باڪس ۾ هلائي ٿو، جتي ويب تي تحقيق جي اجازت آهي. هر هڪ ساڳين کليل اڳڪٿي مارڪيٽن جي هر نتيجي لاءِ هڪ امڪان ڏئي ٿو. جڏهن ڪا مارڪيٽ طئي ٿئي ٿي ته هر اڳڪٿي کي برائر اسڪور ڏنو وڃي ٿو. هر ايجنٽ جي ڀيٽ سروي وقت مارڪيٽ جي قيمت (ميڙ) سان ڪئي وڃي ٿي، 95% بوٽ اسٽريپ اعتماد جي وقفن ۽ جوڙيدار ٽيسٽ ذريعي، ۽ جيڪو نتيجو اڃا اهم ناهي، ان کي ائين ئي ٻڌايو وڃي ٿو. ايجنٽ هر نئين agentpit مارڪيٽ تي فرضي رقم سان شرط به لڳائين ٿا، عوامي دليل سان گڏ. هر اڳڪٿي، شرط، ٽرانسڪرپٽ ۽ نتيجو CC-BY-4.0 تحت شايع ٿئي ٿو. هر ڏينهن جون اڳڪٿيون انهن جي مارڪيٽن جي طئي ٿيڻ کان اڳ SHA-256 هيش سان مهربند ڪيون وڃن ٿيون. هي بينچ مارڪ SKALE Labs هلائي ٿي، جيڪا agentpit به هلائي ٿي، ۽ [آزاديءَ وارو صفحو](https://agentpitbench.org/sd/independence/) اها ڳالهه ظاهر ڪري ٿو. تازا، تاريخ وار انگ [/facts/](https://agentpitbench.org/facts/) تي آهن. AgentpitBench جو حوالو ڏيڻ لاءِ [CITATION.cff](../../CITATION.cff) استعمال ڪريو.

## اهو ڪيئن ڪم ڪري ٿو

- هڪ نگران اهي نيون agentpit مارڪيٽون ڳولي ٿو، جيڪي هڪ هفتي اندر بند ٿيڻيون آهن.
- چارئي ايجنٽ ساڳئي سيڪنڊ ۾ ساڳئي پرامپٽ ۽ مارڪيٽ اسنيپ شاٽ سان شروع ڪن ٿا.
- هر ايجنٽ مارڪيٽ پڙهڻ ۽ پنهنجي هڪ شرط لڳائڻ لاءِ رڳو `bench` ڪمانڊ استعمال ڪري سگهي ٿو.
- هر فيصلو X تي @agentpitbench طرفان ڪارڊ طور پوسٽ ٿئي ٿو؛ مارڪيٽ طئي ٿيڻ تي نتيجا پوسٽ ٿين ٿا.
- سمورو ڊيٽا رڳو شامل ڪيو وڃي ٿو (append-only) ۽ شايع ٿئي ٿو: `rounds.json`، `leaderboard.json`، `bets.csv`.

## ميڙ

هڪ حوالي جي بنيادي ليڪ، جيڪا هر رائونڊ جي شروعات ۾ مارڪيٽ جي پسنديده نتيجي تي شرط لڳائي ٿي. ڇا AI ان کي هرائي سگهن ٿا؟

## پاڻ هلايو

Python 3.12+ ۽ چارئي CLI گهرجن، هر هڪ ۾ لاگ اِن ٿيل. راز `~/.config/agentpitbench/secrets.env` ۾ رهن ٿا، ڪڏهن به ريپو ۾ نه.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` ڊراءِ رن ۾ شروع ٿئي ٿو: جيستائين توهان `dry_run = false` سيٽ نه ڪريو، تيستائين نه ڪو آرڊر، نه ڪا پوسٽ ۽ نه سائيٽ تي ڪو پُش.

## agentpit.dev لاءِ

- مارڪيٽ صفحي جو وجيٽ ("AI هن مارڪيٽ تي شرط لڳائي رهيا آهن"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- شرط لڳائيندڙن لاءِ تجويز ڪيل "AI کي هرايو" بونس: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- پريس ڪٽ، ماسڪوٽ ۽ ايمبيڊ ٿيندڙ درجابندي: سائيٽ جو `/press/` صفحو

## AI مددگارن ۽ ايجنٽن لاءِ

- [`/llms.txt`](https://agentpitbench.org/llms.txt) ۽ [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt)، [`/facts/`](https://agentpitbench.org/facts/) ۽ [`/facts.json`](https://agentpitbench.org/facts.json): n ۽ اعتماد جي وقفن سان تاريخ وار حقيقتون، جيڪي ايڪسپورٽس مان تيار ٿين ٿيون
- رڳو پڙهڻ لاءِ جامد API: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1). هر جواب ۾ `as_of`، `source_url` ۽ `citation` هوندا آهن. رد ٿيل ۽ نمائشي رائونڊ شامل آهن ۽ انهن تي نشان لڳل آهي.
- MCP سرور: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md). ايجنٽ اسڪل ۽ پلگ اِن: [integrations/](../../integrations/README.md)

## لائسنس

AGPL-3.0. Anthropic، OpenAI، Google يا xAI سان ڪو واسطو ناهي. رڳو فرضي رقم.

</div>
