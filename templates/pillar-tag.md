---
name: Content Pillar Tag
description: Which of the creator's pillars it feeds
model: openai/gpt-oss-120b
temperature: 0.1
max_tokens: 140
---

Classify which ONE of the creator's content pillars this reel best feeds: {{pillars}}.
Output exactly two lines:
**Pillar:** <one pillar from the list, or off-pillar>
**Why:** <one short line>

REEL
Author: {{author}}
Format: {{format}} (collection: {{collection}})
Caption: {{caption}}
Transcript: {{transcript}}
