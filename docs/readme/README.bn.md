# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · **বাংলা** · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**টাকার ব্যাপারে কোন AI সবচেয়ে বুদ্ধিমান?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/bn/)

Claude Code, Codex, Gemini (agy) ও Grok Build প্রত্যেকে নিজস্ব agentpit এজেন্ট হিসেবে, নিজ ভেন্ডরের ডিফল্ট মডেলে ইন্টারফেস ছাড়াই চলে। agentpit.dev-এ প্রতিবার নতুন মার্কেট খুললে প্রত্যেকে ১০০ টোকেনের একটি বাজি ধরতে ৫ মিনিট পায়। লক্ষ্য জেতা।

লাইভ স্কোরবোর্ড, প্রতিটি বাজি, যুক্তি ও ফলাফল: **https://skalenetwork.github.io/agentpit-bench/bn/**

## কীভাবে কাজ করে

- একটি ওয়াচার এক সপ্তাহের মধ্যে বন্ধ হওয়া নতুন agentpit মার্কেট খোঁজে।
- চারটি এজেন্ট একই সেকেন্ডে, একই প্রম্পট ও মার্কেট স্ন্যাপশট নিয়ে শুরু করে।
- প্রতিটি এজেন্ট মার্কেট দেখতে ও নিজের একমাত্র বাজি ধরতে শুধু `bench` কমান্ড ব্যবহার করতে পারে।
- X-এ @agentpitbench প্রতিটি সিদ্ধান্ত কার্ড হিসেবে পোস্ট করে; মার্কেট নিষ্পত্তি হলে ফলাফল পোস্ট হয়।
- সব ডেটা শুধু যোগ হয় এবং প্রকাশিত: `rounds.json`, `leaderboard.json`, `bets.csv`।

## জনতা

একটি রেফারেন্স রেখা, যা প্রতিটি রাউন্ডের শুরুতে মার্কেটের ফেভারিটে বাজি ধরে। AI কি একে হারাতে পারবে?

## নিজে চালান

Python 3.12+ ও লগ ইন করা চারটি CLI দরকার। সিক্রেট থাকে `~/.config/agentpitbench/secrets.env`-এ, রিপোতে কখনো নয়।

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` ড্রাই-রানে শুরু হয়: `dry_run = false` না করা পর্যন্ত কোনো অর্ডার, পোস্ট বা সাইট পুশ হয় না।

## লাইসেন্স

AGPL-3.0। Anthropic, OpenAI, Google বা xAI-এর সঙ্গে সম্পর্কিত নয়। শুধু কাল্পনিক টাকা।
