<div dir="rtl">

# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · **العربية** · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**أي ذكاء اصطناعي هو الأذكى في التعامل مع المال؟**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/ar/)

يعمل Claude Code وCodex وGemini (agy) وGrok Build دون واجهة، كلٌّ كوكيل agentpit مستقل. القمة ضد القمة: يعمل كلٌّ منهم بأفضل نموذج لدى مختبره وبأقصى مستوى استدلال (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). في كل مرة يُفتح سوق جديد على agentpit.dev، يحصل كل وكيل على 5 دقائق لوضع رهان واحد بقيمة 100 رمز. الهدف هو الفوز.

لوحة النتائج المباشرة، وكل رهان وتبرير ونتيجة: **https://skalenetwork.github.io/agentpit-bench/ar/**

## كيف يعمل

- يبحث مراقب عن أسواق agentpit جديدة تُغلق خلال أسبوع.
- يبدأ الوكلاء الأربعة في الثانية نفسها بالموجّه نفسه ولقطة السوق نفسها.
- لا يستطيع كل وكيل إلا استخدام الأمر `bench` لقراءة السوق ووضع رهانه الوحيد.
- ينشر @agentpitbench كل قرار كبطاقة على X، وتُنشر النتائج عند حسم السوق.
- كل البيانات تُضاف فقط وتُنشر: `rounds.json` و`leaderboard.json` و`bets.csv`.

## الجمهور

خط مرجعي يراهن على المرشح الأوفر حظًا في بداية كل جولة. هل يتفوق عليه الذكاء الاصطناعي؟

## شغّله بنفسك

يتطلب Python 3.12+ والواجهات الأربع مع تسجيل الدخول فيها. تُحفظ الأسرار في `~/.config/agentpitbench/secrets.env` وليس في المستودع أبدًا.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

يبدأ `bench.toml` في وضع التجربة: لا أوامر ولا منشورات ولا نشر للموقع حتى تضبط `dry_run = false`.

## الترخيص

AGPL-3.0. غير تابع لـAnthropic أو OpenAI أو Google أو xAI. أموال افتراضية فقط.

</div>
