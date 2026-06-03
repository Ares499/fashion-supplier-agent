# Portfolio Brief

## Project

Fashion Supplier Selection Agent

## One-Line Pitch

An automation agent that turns supplier chat images into selectable buying reports and structured product-operation sheets.

## What It Demonstrates

- Real-world workflow modeling with a recoverable daily state machine.
- WeCom message archive integration for image and text ingestion.
- Desktop automation fallback for message sending during transition periods.
- Contact identity reconciliation between desktop conversation names and official `external_userid`.
- Image deduplication, product card generation, report rendering, and selection detection.
- Human-in-the-loop controls for risky actions.
- Failure diagnosis, alerting, and recovery reconciliation.
- Unit-tested business logic across ingestion, reporting, selection, messaging, and recovery flows.

## Demo Flow

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m supplier_bot.cli init
.venv/bin/python -m supplier_bot.cli seed-demo
.venv/bin/python -m supplier_bot.cli import-demo-images --date 2026-05-21
.venv/bin/python -m supplier_bot.cli build-report --date 2026-05-21
.venv/bin/python -m supplier_bot.cli make-demo-selection --date 2026-05-21
.venv/bin/python -m supplier_bot.cli detect-selection --date 2026-05-21 --screenshot data/reports/2026-05-21/demo_selection.png
```

## How To Explain It In An Interview

I built an automation agent for a clothing ecommerce workflow. Suppliers send daily product images through enterprise chat, the system ingests and classifies those images, generates a buying report, detects which products were selected from a marked screenshot, and then prepares supplier sample requests plus an operations spreadsheet.

The important engineering part is that the workflow is not a one-off script. It is a state machine with persistent files and a local database, so it can recover after a restart. It also handles the messy real-world issue where sending may happen through a desktop client while receiving happens through an official SDK. I added proof-based contact binding using SDK outgoing messages and unique batch markers so unknown sender images do not silently map to the wrong supplier.

## Current Limitations

- Full official send-and-receive flow depends on enterprise permissions, trusted IP/domain configuration, and compliance setup.
- The public repository contains demo data only. Production data, message archives, contacts, logs, and secrets are intentionally excluded.
- Vision classification quality depends on the configured model provider; local rules are intentionally conservative.

## Next Steps

- Move supplier-facing sends to official APIs when enterprise permissions are ready.
- Add a public demo video or screenshots generated from demo data.
- Split deployment docs for local desktop, server SDK polling, and all-official modes.

