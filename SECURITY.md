# Security And Data Handling

This repository is intended to be safe for portfolio and collaboration review.

## Do Not Commit

Never commit:

- `.env` or any environment-specific config
- WeCom corp secrets, app secrets, callback tokens, EncodingAESKey values
- WeCom message archive private keys
- SDK binary libraries
- SMTP passwords
- local databases
- contact lists
- chat callback events
- supplier images
- generated reports or spreadsheets
- logs

The default `.gitignore` excludes these paths:

```text
data/**
logs/
outputs/
vendor/
.env
*.pem
*.key
*.sqlite3
```

## Pre-Push Checklist

Run these checks before publishing:

```bash
git status --short
rg -n "SECRET|TOKEN|PASSWORD|PRIVATE_KEY|BEGIN RSA|BEGIN PRIVATE|wm2w0|wo2w0|admin@|\\.pem" .
```

Expected result:

- `git status` should only show source code, docs, tests, and safe examples.
- the `rg` command may find config variable names in source code, but should not find real secret values.

## Runtime Data

Runtime data is generated locally under `data/`. This includes the SQLite database, image inbox, workflow state, reports, selections, and message archive state. These files are intentionally excluded from Git.

## Demo Data

Use the built-in demo commands for public screenshots or walkthroughs:

```bash
.venv/bin/python -m supplier_bot.cli init
.venv/bin/python -m supplier_bot.cli seed-demo
.venv/bin/python -m supplier_bot.cli import-demo-images --date 2026-05-21
.venv/bin/python -m supplier_bot.cli build-report --date 2026-05-21
```

