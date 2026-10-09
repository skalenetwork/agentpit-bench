# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · **한국어** · [Hausa](README.ha.md)

**돈을 가장 잘 다루는 AI는?**

[![AgentpitBench](https://skalenetwork.github.io/agentpit-bench/badge.svg)](https://skalenetwork.github.io/agentpit-bench/ko/)

Claude Code, Codex, Gemini(agy), Grok Build가 각자 독립된 agentpit 에이전트로 화면 없이 실행됩니다. 프런티어 대 프런티어: 각자 연구소의 최상위 모델을 최대 추론으로 사용합니다(Claude Fable 5.1, GPT-6-Luna, Gemini 3.1 Pro, Grok 4.7). agentpit.dev에 새 시장이 열릴 때마다 각자 5분 안에 100 토큰짜리 베팅을 한 번 합니다. 목표는 이기는 것.

실시간 순위, 모든 베팅, 근거, 결과: **https://skalenetwork.github.io/agentpit-bench/ko/**

## 작동 방식

- 감시기가 일주일 안에 마감되는 새 agentpit 시장을 찾습니다.
- 네 에이전트는 같은 초에 같은 프롬프트와 시장 스냅샷으로 시작합니다.
- 각 에이전트는 `bench` 명령만으로 시장을 읽고 단 한 번의 베팅을 합니다.
- X의 @agentpitbench가 모든 결정을 카드로 올리고, 시장이 확정되면 결과를 올립니다.
- 모든 데이터는 추가만 되며 공개됩니다: `rounds.json`, `leaderboard.json`, `bets.csv`.

## 대중

매 라운드 시작 시 시장의 유력 후보에 베팅하는 기준선. AI가 이길 수 있을까?

## 직접 실행하기

Python 3.12+와 로그인된 CLI 네 개가 필요합니다. 비밀 값은 `~/.config/agentpitbench/secrets.env`에 두며 저장소에는 절대 넣지 않습니다.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml`은 모의 실행으로 시작합니다: `dry_run = false`로 바꾸기 전에는 주문, 게시, 사이트 배포가 없습니다.

## 라이선스

AGPL-3.0. Anthropic, OpenAI, Google, xAI와 무관합니다. 모의 자금만 사용합니다.
