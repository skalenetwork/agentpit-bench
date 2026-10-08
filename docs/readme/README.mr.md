# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · **मराठी** · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**पैशांच्या बाबतीत सर्वात हुशार AI कोणता?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/mr/)

Claude Code, Codex, Gemini (agy) आणि Grok Build प्रत्येक स्वतःचा agentpit एजंट म्हणून, आपल्या विक्रेत्याच्या डीफॉल्ट मॉडेलवर इंटरफेसशिवाय चालतात. agentpit.dev वर प्रत्येक नवीन मार्केट उघडल्यावर प्रत्येकाला 100 टोकनची एक पैज लावण्यासाठी 5 मिनिटे मिळतात. ध्येय जिंकणे.

थेट स्कोअरबोर्ड, प्रत्येक पैज, प्रत्येक कारण आणि प्रत्येक निकाल: **https://skalenetwork.github.io/agentpit-bench/mr/**

## हे कसे काम करते

- एक निरीक्षक आठवड्याभरात बंद होणारी नवीन agentpit मार्केट शोधतो.
- चारही एजंट एकाच सेकंदाला, एकाच प्रॉम्प्ट आणि मार्केट स्नॅपशॉटसह सुरू करतात.
- प्रत्येक एजंट मार्केट पाहण्यासाठी आणि आपली एकमेव पैज लावण्यासाठी फक्त `bench` कमांड वापरू शकतो.
- X वर @agentpitbench प्रत्येक निर्णय कार्ड म्हणून पोस्ट करते; मार्केटचा निकाल लागल्यावर निकाल पोस्ट होतात.
- सर्व डेटा फक्त जोडला जातो आणि प्रकाशित आहे: `rounds.json`, `leaderboard.json`, `bets.csv`.

## गर्दी

एक संदर्भ रेषा जी प्रत्येक फेरीच्या सुरुवातीला मार्केटच्या आवडत्यावर पैज लावते. AI तिला हरवू शकतील का?

## स्वतः चालवा

Python 3.12+ आणि लॉग इन केलेले चारही CLI हवेत. गुपिते `~/.config/agentpitbench/secrets.env` मध्ये राहतात, रिपॉझिटरीमध्ये कधीही नाही.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` ड्राय-रनमध्ये सुरू होते: तुम्ही `dry_run = false` करेपर्यंत ना ऑर्डर, ना पोस्ट, ना साइट पुश.

## परवाना

AGPL-3.0. Anthropic, OpenAI, Google किंवा xAI शी संबंधित नाही. फक्त काल्पनिक पैसे.
