# Hindsight Obsidian Sync

Simple scripts for syncing Markdown notes from an Obsidian vault into a local Hindsight instance.

## What this repo does

- Runs Hindsight + Postgres (with pgvector) using Docker Compose
- Syncs `.md` files from your vault into a Hindsight memory bank
- Tracks synced files in a local manifest so unchanged notes are skipped
- Optionally deletes documents in Hindsight when notes are removed locally

## Requirements

- Docker + Docker Compose
- Python 3.10+
- An Obsidian vault on your machine

## Setup

1. Install Python dependency:

```bash
pip install -r requirements.txt
```

2. Start Hindsight services:

```bash
docker compose up -d
```

3. (Optional) Confirm API is running at:

`http://localhost:8888`

## Sync commands

Main script:

```bash
python scripts/sync_obsidian.py --source "path/in/vault"
```

Included helpers:

- `./sync_daily.sh` syncs `00-09 System/00.01 Daily Notes`
- `./sync_entities.sh` syncs `10-19 Entities`

## Common options

- `--vault-root` (default: `~/Documents/notes`)
- `--source` folder to sync (relative to vault root or absolute)
- `--bank` Hindsight memory bank id (default: `obsidian`)
- `--batch-size` number of changed notes per request
- `--async` queue work and return immediately
- `--delete-missing` remove docs in Hindsight for deleted local files
- `--dry-run` preview changes without sending anything

## Notes

- Sync state is stored in `.obsidian-hindsight-manifest.json`
- By default, files are not deleted from Hindsight unless you pass `--delete-missing`
- Script currently syncs Markdown files only (`.md`)
