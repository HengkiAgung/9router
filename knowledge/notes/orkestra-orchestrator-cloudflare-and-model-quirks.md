---
title: Orkestra orchestrator: Cloudflare and model quirks
created: 2026-11-03
tags: [orkestra, 9router, cloudflare, quirk]
---

# Orkestra orchestrator: Cloudflare and model quirks

tools/orchestrator.py calls 9router at https://airouter.hengkiagung.my.id/v1. As of 2026-11-03 the domain is proxied by Cloudflare (it was DNS-only at deploy time). Cloudflare blocks the default Python-urllib User-Agent with HTTP 403 'error code: 1010', so any script calling the router must send its own User-Agent. The Cloudflare free plan also cuts proxied requests at 100s (HTTP 524), and kenari free models sometimes take 75-124s, so long non-streaming calls can fail through the domain but not via 127.0.0.1:20128. ag/gemini-3.8-flash sometimes ends a turn with neither text nor a tool call; the orchestrator nudges once and then switches model. ag/claude-sonnet-5-5-* returns 404 for this Antigravity account; ag/claude-sonnet-4-6 and ag/gemini-3.8-flash work.
