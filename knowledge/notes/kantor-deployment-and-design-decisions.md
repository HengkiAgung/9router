---
title: Kantor deployment and design decisions
created: 2026-11-03
tags: [kantor, deployment, security, 9router]
---

# Kantor deployment and design decisions

Kantor (tools/kantor/) is live at https://myoffice.hengkiagung.my.id (Cloudflare proxied) as systemd service kantor on 127.0.0.1:4610, deployed by tools/kantor/deploy.sh from the repo .env. Workers are threads calling 9router locally (http://127.0.0.1:20128/v1, which avoids Cloudflare's 100s limit). It runs as user kantor (no sudo) with ProtectHome=tmpfs + BindPaths=/home/kantor, ProtectSystem=strict, NoNewPrivileges, MemoryMax 512M; secrets are in /etc/kantor/kantor.env (root 600, read by systemd), and only a scrypt hash of the password leaves .env. The hash uses ':' separators because systemd environment files may expand '$'. Commands run with a clean environment, but an approved command could still read /proc/<kantor pid>/environ (same uid), so read commands before approving them. Deploy pitfall: sudo -u kantor commands must not start in /home/ubuntu (750), so deploy.sh does cd / first. First end-to-end test (2026-11-03): a manager hired 2 workers, merged their branches and passed the tests in 55s.
