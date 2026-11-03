---
title: Kantor code graphs: memory limits and what to ignore
created: 2026-11-03
tags: [kantor, graphify, memory, karunia]
---

# Kantor code graphs: memory limits and what to ignore

Measured 2026-11-03 on the 2 GB server. graphify extract --code-only is AST only (no LLM): a few hundred files take ~70 MB and seconds. karir took 514 MB until public/ minified JS was ignored (then 65 MB). kai-erp (Laravel, 12,590 code files) ran out of memory in one piece even with --max-workers 1, and its database/seeders/MsSparepartsTableSeeder.php is a 28 MB data dump that alone exhausted memory; resources/views (4,140 Blade files) adds noise. So Kantor ignores assets, third-party code, views, seeders and files over 1 MB, and builds repos over 1500 code files per folder (kai-erp: 30 parts, 4.5 min, Kantor peak 399 MB). Merging kai-erp's parts into one graph made 71k nodes and needed 475 MB, too heavy to query, so parts stay separate behind KANTOR_INDEX.md. cluster-only costs ~300 MB regardless of size. graphify label auto-detects the claude CLI (not logged in for kantor), so labels go to 9router with --backend=openai and OPENAI_BASE_URL. A query on the 19k-node app/Core part takes 1.5 s and 130 MB.
