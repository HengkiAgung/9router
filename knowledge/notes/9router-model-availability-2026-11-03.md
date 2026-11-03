---
title: 9router model availability (2026-11-03)
created: 2026-11-03
tags: [9router, models, antigravity, ollama, kenari]
---

# 9router model availability (2026-11-03)

Tested through 9router with tool calling. Work: ag/gemini-3.8-flash, ag/gemini-3.8-flash-high, ag/claude-sonnet-4-6, ag/claude-opus-4-6-thinking, ag/gpt-oss-120b-medium (40s, slow), ollama/gpt-oss:120b, ollama/gemma4:31b (Ollama free plan), ollama/beam, kenari/*:free (rate limited per minute). 404 on this Antigravity account: all ag/gemini-3.1-pro-*, ag/gemini-4-argon-*, ag/claude-sonnet-5-5-*, ag/claude-opus-5-5. Ollama retired (410): glm-5, kimi-k2.5, minimax-m2.5, qwen3.5. Ollama 402 (needs usage credits, so treated as paid): minimax-m3, deepseek-v4.1-flash:cloud. ollama/clef* only work on /v1/systemone; oc/* free models reject the chat-completions protocol. Kenari paid models fail with 'not allowed by this key' on the free key. Kantor tiers in .env are based on this list.
