---
name: Hook Teardown
description: The opening line, its hook type, and 3-second stopping power
model: openai/gpt-oss-120b
temperature: 0.3
max_tokens: 460
---

Break down ONLY the hook (first 3 seconds) of this reel.
**Opening line/visual:** quote the exact first spoken line (from the transcript) and describe the opening visual if inferable.
**Hook type:** classify it (bold claim / question / pattern interrupt / proof-first / curiosity gap / negativity / us-vs-them). One line on why.
**Stopping power (1-10):** rate how hard it stops the scroll and say what does the work.
**Steal it:** the hook as a reusable fill-in-the-blank template the creator could apply to their topics.

REEL
Author: {{author}}
Format: {{format}} (collection: {{collection}})
Caption: {{caption}}
Transcript: {{transcript}}
