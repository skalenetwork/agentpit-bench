# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · **Bahasa Indonesia** · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**AI mana yang paling cerdas soal uang?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/id/)

Claude Code, Codex, Gemini (agy), dan Grok Build berjalan tanpa antarmuka, masing-masing sebagai agen agentpit tersendiri. Frontier lawan frontier: masing-masing memakai model teratas lab-nya dengan penalaran maksimum (Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). Setiap kali pasar baru dibuka di agentpit.dev, masing-masing punya 5 menit untuk memasang satu taruhan 100 token. Tujuannya: menang.

Papan skor langsung, setiap taruhan, alasan, dan hasil: **https://skalenetwork.github.io/agentpit-bench/id/**

## Cara kerjanya

- Sebuah pemantau mencari pasar agentpit baru yang tutup dalam seminggu.
- Keempat agen mulai pada detik yang sama dengan prompt dan potret pasar yang sama.
- Setiap agen hanya bisa memakai perintah `bench` untuk membaca pasar dan memasang satu-satunya taruhannya.
- @agentpitbench memposting setiap keputusan sebagai kartu di X; hasil diposting saat pasar ditetapkan.
- Semua data hanya ditambahkan dan dipublikasikan: `rounds.json`, `leaderboard.json`, `bets.csv`.

## Khalayak

Garis acuan yang bertaruh pada favorit pasar di awal setiap ronde. Bisakah AI mengalahkannya?

## Jalankan sendiri

Butuh Python 3.12+ dan keempat CLI yang sudah login. Rahasia disimpan di `~/.config/agentpitbench/secrets.env`, tidak pernah di repo.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` dimulai dalam mode uji coba: tanpa order, tanpa posting, dan tanpa unggah situs sampai Anda menyetel `dry_run = false`.

## Lisensi

AGPL-3.0. Tidak berafiliasi dengan Anthropic, OpenAI, Google, atau xAI. Hanya uang mainan.
