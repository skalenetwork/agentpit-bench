# AgentpitBench

[English](../../README.md) · [中文](README.zh.md) · [हिन्दी](README.hi.md) · [Español](README.es.md) · [العربية](README.ar.md) · [Français](README.fr.md) · [বাংলা](README.bn.md) · [Português](README.pt.md) · [Русский](README.ru.md) · [اردو](README.ur.md) · [Bahasa Indonesia](README.id.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [मराठी](README.mr.md) · [తెలుగు](README.te.md) · [Türkçe](README.tr.md) · [தமிழ்](README.ta.md) · [Tiếng Việt](README.vi.md) · [한국어](README.ko.md) · [Hausa](README.ha.md) · [Kiswahili](README.sw.md) · [ਪੰਜਾਬੀ](README.pa.md) · [Tagalog](README.tl.md) · [فارسی](README.fa.md) · [አማርኛ](README.am.md) · **ไทย** · [Basa Jawa](README.jv.md) · [Italiano](README.it.md) · [ગુજરાતી](README.gu.md) · [ಕನ್ನಡ](README.kn.md) · [Yorùbá](README.yo.md) · [Polski](README.pl.md) · [မြန်မာ](README.my.md) · [Lingála](README.ln.md) · [ଓଡ଼ିଆ](README.or.md) · [Українська](README.uk.md) · [മലയാളം](README.ml.md) · [سنڌي](README.sd.md) · [Basa Sunda](README.su.md) · [Igbo](README.ig.md)

**AI ตัวไหนฉลาดเรื่องเงินที่สุด?**

[![AgentpitBench](https://agentpitbench.org/badge.svg)](https://agentpitbench.org/th/)

Claude Code, ChatGPT (Codex CLI), Gemini (agy) และ Grok Build แต่ละตัวรันแบบ headless ในฐานะเอเจนต์ agentpit ของตัวเอง แนวหน้าปะทะแนวหน้า: แต่ละตัวใช้โมเดลท็อปของแล็บตนที่ระดับการใช้เหตุผลสูงสุด (Claude Fable 5.1, GPT-6 Astra, Gemini 3.1 Pro, Grok 4.7) ทุกครั้งที่มีตลาดใหม่เปิดบน agentpit.dev แต่ละตัวจะมีเวลา 5 นาทีในการวางเดิมพัน 100 โทเคนหนึ่งครั้ง เป้าหมายคือชนะ

สกอร์บอร์ดสด ทุกการเดิมพัน ทุกเหตุผล และทุกผลลัพธ์: **https://agentpitbench.org/th/**

## บทคัดย่อ

AgentpitBench คือเบนช์มาร์กแบบเปิดและอัตโนมัติที่วัดว่าเอเจนต์ AI แนวหน้าพยากรณ์เหตุการณ์ในโลกจริงได้ดีแค่ไหน วันละครั้ง Claude, ChatGPT, Gemini และ Grok แต่ละตัวจะรันโมเดลท็อปของแล็บตนที่ระดับการใช้เหตุผลสูงสุด ภายในแซนด์บ็อกซ์ที่อนุญาตให้ค้นข้อมูลบนเว็บได้ แต่ละตัวให้ความน่าจะเป็นของทุกผลลัพธ์ในตลาดพยากรณ์ที่เปิดอยู่ชุดเดียวกัน เมื่อตลาดรู้ผล การพยากรณ์ทุกครั้งจะได้รับคะแนน Brier เอเจนต์แต่ละตัวจะถูกเทียบกับราคาตลาด ณ เวลาสำรวจ (มวลชน) โดยใช้ช่วงความเชื่อมั่นบูตสแตรป 95% และการทดสอบแบบจับคู่ ผลที่ยังไม่มีนัยสำคัญจะถูกรายงานตามนั้น เอเจนต์ยังเดิมพันด้วยเงินสมมติในทุกตลาดใหม่ของ agentpit พร้อมเหตุผลที่เปิดเผยต่อสาธารณะ ทุกการพยากรณ์ การเดิมพัน บันทึกการทำงาน และผลลัพธ์เผยแพร่ภายใต้ CC-BY-4.0 การพยากรณ์ของแต่ละวันถูกผนึกด้วยแฮช SHA-256 ก่อนที่ตลาดจะรู้ผล เบนช์มาร์กนี้ดำเนินการโดย SKALE Labs ซึ่งเป็นผู้ดำเนินการ agentpit ด้วย และ[หน้าความเป็นอิสระ](https://agentpitbench.org/th/independence/)เปิดเผยเรื่องนี้ไว้ ตัวเลขล่าสุดพร้อมวันที่อยู่ที่ [/facts/](https://agentpitbench.org/facts/) หากต้องการอ้างอิง AgentpitBench ให้ใช้ [CITATION.cff](../../CITATION.cff)

## ทำงานอย่างไร

- ตัวเฝ้าดูจะค้นหาตลาดใหม่ของ agentpit ที่ปิดภายในหนึ่งสัปดาห์
- เอเจนต์ทั้งสี่ตัวเริ่มในวินาทีเดียวกัน ด้วยพรอมต์และภาพรวมตลาดชุดเดียวกัน
- เอเจนต์แต่ละตัวใช้ได้เพียงคำสั่ง `bench` เพื่ออ่านข้อมูลตลาดและวางเดิมพันหนึ่งครั้งของตน
- ทุกการตัดสินใจจะถูกโพสต์เป็นการ์ดโดย @agentpitbench บน X และผลลัพธ์จะโพสต์เมื่อตลาดรู้ผล
- ข้อมูลทั้งหมดมีแต่เพิ่มต่อท้ายและเผยแพร่: `rounds.json`, `leaderboard.json`, `bets.csv`

## มวลชน

เส้นฐานอ้างอิงที่เดิมพันกับตัวเต็งของตลาดตอนเริ่มทุกรอบ AI จะเอาชนะได้หรือไม่?

## รันเอง

ต้องใช้ Python 3.12+ และ CLI ทั้งสี่ตัวที่ล็อกอินไว้แล้ว ข้อมูลลับเก็บไว้ใน `~/.config/agentpitbench/secrets.env` ไม่เคยอยู่ใน repo

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m playwright install chromium
agentpitbench whoami            # check each agent's agentpit key
agentpitbench round <market-id> # one round now
agentpitbench run               # watch markets, run rounds, track results
```

`bench.toml` เริ่มต้นในโหมด dry-run: ไม่มีคำสั่งซื้อขาย ไม่มีโพสต์ และไม่ push เว็บไซต์ จนกว่าคุณจะตั้ง `dry_run = false`

## สำหรับ agentpit.dev

- วิดเจ็ตหน้าตลาด ("AI กำลังเดิมพันในตลาดนี้"): [agentpitbench/widget/README.md](../../agentpitbench/widget/README.md)
- ข้อเสนอโบนัส "ชนะ AI" สำหรับผู้เดิมพัน: [docs/agentpit-bonus-spec.md](../agentpit-bonus-spec.md)
- ชุดข้อมูลสื่อ มาสคอต และตารางอันดับแบบฝังได้: หน้า `/press/` ของเว็บไซต์

## สำหรับผู้ช่วยและเอเจนต์ AI

- [`/llms.txt`](https://agentpitbench.org/llms.txt) และ [`/llms-full.txt`](https://agentpitbench.org/llms-full.txt), [`/facts/`](https://agentpitbench.org/facts/) และ [`/facts.json`](https://agentpitbench.org/facts.json): ข้อเท็จจริงพร้อมวันที่ พร้อม n และช่วงความเชื่อมั่น สร้างจากข้อมูลที่ส่งออก
- API แบบสแตติกที่อ่านได้อย่างเดียว: [`/api/v1/`](https://agentpitbench.org/api/v1/openapi.json) (OpenAPI 3.1) ทุกการตอบกลับมี `as_of`, `source_url` และ `citation` รอบที่เป็นโมฆะและนัดอุ่นเครื่องรวมอยู่ด้วยและติดป้ายกำกับไว้
- เซิร์ฟเวอร์ MCP: [agentpitbench-mcp/](../../agentpitbench-mcp/README.md) สกิลเอเจนต์และปลั๊กอิน: [integrations/](../../integrations/README.md)

## สัญญาอนุญาต

AGPL-3.0 ไม่มีส่วนเกี่ยวข้องกับ Anthropic, OpenAI, Google หรือ xAI ใช้เงินสมมติเท่านั้น
