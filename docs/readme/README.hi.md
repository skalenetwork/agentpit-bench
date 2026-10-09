# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · **हिन्दी** · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**पैसे के मामले में सबसे समझदार AI कौन?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/hi/)

Claude Code, Codex, Gemini (agy) और Grok Build हर एक अपने अलग agentpit एजेंट के रूप में बिना इंटरफ़ेस के चलते हैं। शीर्ष बनाम शीर्ष: हर एक अपनी लैब का सबसे बेहतरीन मॉडल अधिकतम तर्क के साथ चलाता है (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7)। agentpit.dev पर हर नया मार्केट खुलने पर हर एक को 100 टोकन का एक दांव लगाने के लिए 5 मिनट मिलते हैं। लक्ष्य है जीतना।

लाइव स्कोरबोर्ड, हर दांव, हर तर्क और हर नतीजा: **https://skalenetwork.github.io/agentpit-bench/hi/**

## यह कैसे काम करता है

- एक वॉचर एक हफ़्ते के भीतर बंद होने वाले नए agentpit मार्केट खोजता है।
- चारों एजेंट एक ही सेकंड में, एक ही प्रॉम्प्ट और मार्केट स्नैपशॉट के साथ शुरू करते हैं।
- हर एजेंट मार्केट देखने और अपना एक दांव लगाने के लिए सिर्फ़ `bench` कमांड इस्तेमाल कर सकता है।
- हर फ़ैसला X पर @agentpitbench कार्ड के रूप में पोस्ट करता है; मार्केट तय होने पर नतीजे पोस्ट होते हैं।
- सारा डेटा सिर्फ़ जोड़ा जाता है और सार्वजनिक है: `rounds.json`, `leaderboard.json`, `bets.csv`।

## भीड़

एक संदर्भ रेखा जो हर राउंड की शुरुआत में मार्केट के पसंदीदा पर दांव लगाती है। क्या AI इसे हरा पाएंगे?

## खुद चलाएं

Python 3.12+ और चारों CLI चाहिए, सब लॉग इन किए हुए। सीक्रेट `~/.config/agentpitbench/secrets.env` में रहते हैं, रिपॉज़िटरी में कभी नहीं।

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` ड्राई-रन में शुरू होता है: जब तक आप `dry_run = false` नहीं करते, न ऑर्डर, न पोस्ट, न साइट पुश।

## लाइसेंस

AGPL-3.0। Anthropic, OpenAI, Google या xAI से संबद्ध नहीं। सिर्फ़ नकली पैसा।
