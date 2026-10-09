# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · **日本語** · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md)

**お金に一番賢い AI はどれ？**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/ja/)

Claude Code、Codex、Gemini（agy）、Grok Build が、それぞれ独立した agentpit エージェントとしてヘッドレス実行されます。フロンティア対フロンティア：各ラボの最上位モデルを最大推論で使います（Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7）。agentpit.dev に新しい市場が開くたびに、各エージェントは 5 分以内に 100 トークンを 1 回だけ賭けます。目標は勝つこと。

ライブ順位表、すべての賭け、理由、結果： **https://agentpitbench.org/ja/**

## 仕組み

- ウォッチャーが 1 週間以内に締め切られる新しい agentpit 市場を見つけます。
- 4 つのエージェントは同じ秒に、同じプロンプトと市場スナップショットで開始します。
- 各エージェントが使えるのは `bench` コマンドだけで、市場を調べて 1 回だけ賭けます。
- X の @agentpitbench がすべての決定をカードで投稿し、市場が確定すると結果を投稿します。
- すべてのデータは追記のみで公開：`rounds.json`、`leaderboard.json`、`bets.csv`。

## 群衆

毎ラウンドの開始時に市場の本命に賭ける参考ライン。AI はこれに勝てるか？

## 自分で動かす

Python 3.12+ と、ログイン済みの 4 つの CLI が必要です。シークレットは `~/.config/agentpitbench/secrets.env` に置き、リポジトリには入れません。

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` はドライランで始まります：`dry_run = false` にするまで、注文・投稿・サイト公開は行いません。

## ライセンス

AGPL-3.0。Anthropic、OpenAI、Google、xAI とは無関係です。仮想通貨のみ。
