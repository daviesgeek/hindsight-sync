#!/usr/bin/env python3
"""Sync an Obsidian Markdown vault into a Hindsight memory bank.

The script runs on the host machine, reads local Markdown files, and talks to a
Dockerized Hindsight API over HTTP. It keeps a local manifest so repeated runs
only retain changed notes and delete Hindsight documents for removed notes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


DEFAULT_API_URL = "http://localhost:8888"
DEFAULT_BANK_ID = "obsidian"
DEFAULT_VAULT_ROOT = "~/Documents/notes"
DEFAULT_MANIFEST_PATH = ".obsidian-hindsight-manifest.json"
DEFAULT_BATCH_SIZE = 25

# File extensions to sync from the Obsidian vault.
INCLUDED_FILE_EXTENSIONS = [".md"]

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
WIKILINK_RE = re.compile(r"!??\[\[([^\]]+)\]\]")
HASH_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_/-]+)")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
COMPACT_DATE_RE = re.compile(r"\b(\d{4})(\d{2})(\d{2})\b")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync an Obsidian vault into a Hindsight memory bank."
    )
    parser.add_argument(
        "--vault-root",
        default=DEFAULT_VAULT_ROOT,
        help="Full Obsidian vault root used for durable vault-relative document IDs",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Folder to scan. Relative paths are resolved inside --vault-root. Defaults to --vault-root.",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="Deprecated alias for --source",
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Hindsight API URL")
    parser.add_argument("--bank", default=DEFAULT_BANK_ID, help="Hindsight bank ID")
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_PATH,
        help="Local manifest path, relative to current directory unless absolute",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Number of changed notes per retain request",
    )
    parser.add_argument(
        "--sync",
        dest="sync_mode",
        action="store_true",
        default=True,
        help="Wait for retain processing to complete before returning. This is the default.",
    )
    parser.add_argument(
        "--async",
        dest="sync_mode",
        action="store_false",
        help="Queue retain operations in Hindsight and return immediately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned retains/deletes without calling Hindsight or updating the manifest",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retain every note even when the manifest says it is unchanged",
    )
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Delete Hindsight documents for missing files, scoped only to the active source",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="Deprecated no-op. Deletion is disabled unless --delete-missing is provided.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP request timeout in seconds",
    )
    return parser.parse_args()


def resolve_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def resolve_source_path(vault_root: Path, source: str | None) -> Path:
    if not source:
        return vault_root
    expanded = Path(os.path.expandvars(os.path.expanduser(source)))
    if expanded.is_absolute():
        return expanded.resolve()
    return (vault_root / expanded).resolve()


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def source_id(vault_root: Path, source_path: Path) -> str:
    value = f"{vault_root}\n{source_path}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "banks": {}}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Manifest has invalid shape: {path}")
    return data


def manifest_source(
    manifest: dict[str, Any],
    bank_id: str,
    vault_root: Path,
    source_path: Path,
) -> dict[str, Any]:
    sid = source_id(vault_root, source_path)
    if "banks" not in manifest:
        manifest["version"] = 2
        manifest["banks"] = {}

    bank = manifest.setdefault("banks", {}).setdefault(bank_id, {"sources": {}})
    sources = bank.setdefault("sources", {})

    # Migrate a legacy single-source manifest into its own source entry. This
    # preserves existing document IDs even if the current run targets a
    # different source folder.
    if "notes" in manifest and isinstance(manifest.get("notes"), dict):
        legacy_path = manifest.get("vault_path")
        if legacy_path:
            legacy_source_path = resolve_path(str(legacy_path))
            legacy_vault_root = vault_root if path_is_relative_to(legacy_source_path, vault_root) else legacy_source_path
            legacy_sid = source_id(legacy_vault_root, legacy_source_path)
            sources.setdefault(legacy_sid, {
                "vault_root": str(legacy_vault_root),
                "source_path": str(legacy_source_path),
                "source_relative_path": source_relative_path(legacy_vault_root, legacy_source_path),
                "legacy_document_ids": True,
                "notes": manifest["notes"],
            })
        else:
            sources.setdefault(sid, {
                "vault_root": str(vault_root),
                "source_path": str(source_path),
                "source_relative_path": source_relative_path(vault_root, source_path),
                "legacy_document_ids": True,
                "notes": manifest["notes"],
            })
        del manifest["notes"]

    source = sources.setdefault(
        sid,
        {
            "vault_root": str(vault_root),
            "source_path": str(source_path),
            "source_relative_path": source_relative_path(vault_root, source_path),
            "legacy_document_ids": False,
            "notes": {},
        },
    )
    source.setdefault("notes", {})
    source.setdefault("legacy_document_ids", False)
    source["vault_root"] = str(vault_root)
    source["source_path"] = str(source_path)
    source["source_relative_path"] = source_relative_path(vault_root, source_path)
    return source


def source_relative_path(vault_root: Path, source_path: Path) -> str:
    if source_path == vault_root:
        return "."
    return source_path.relative_to(vault_root).as_posix()


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def iter_sync_files(source_path: Path) -> list[Path]:
    return sorted(
        path
        for path in source_path.rglob("*")
        if path.is_file()
        and path.suffix in INCLUDED_FILE_EXTENSIONS
        and ".obsidian" not in path.relative_to(source_path).parts
    )


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw = match.group(1)
    body = text[match.end() :]
    return parse_simple_yaml(raw), body


def parse_simple_yaml(raw: str) -> dict[str, Any]:
    """Parse common Obsidian frontmatter without a YAML dependency.

    This intentionally handles only simple `key: value` and inline arrays. The
    original content is still retained, so imperfect metadata parsing does not
    lose note content.
    """
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if current_key and stripped.startswith("-"):
            data.setdefault(current_key, []).append(clean_scalar(stripped[1:].strip()))
            continue

        if ":" not in line:
            current_key = None
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key

        if value == "":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            values = [part.strip() for part in value[1:-1].split(",") if part.strip()]
            data[key] = [clean_scalar(part) for part in values]
        else:
            data[key] = clean_scalar(value)

    return data


def clean_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def extract_frontmatter_tags(frontmatter: dict[str, Any]) -> list[str]:
    raw_tags = frontmatter.get("tags") or frontmatter.get("tag") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, list):
        return []

    tags: list[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        tags.extend(part.strip().lstrip("#") for part in tag.split() if part.strip())
    return sorted(set(tags))


def extract_hash_tags(text: str) -> list[str]:
    return sorted(set(match.group(1) for match in HASH_TAG_RE.finditer(text)))


def extract_wikilinks(text: str) -> list[str]:
    links: set[str] = set()
    for match in WIKILINK_RE.finditer(text):
        target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            links.add(target)
    return sorted(links)


def normalize_tag(value: str) -> str:
    return value.strip().replace(" ", "-")


def iso_from_timestamp(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_datetime(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        date_match = ISO_DATE_RE.search(text) or COMPACT_DATE_RE.search(text)
        if not date_match:
            return None
        year, month, day = (int(part) for part in date_match.groups())
        parsed = dt.datetime(year, month, day)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    else:
        parsed = parsed.astimezone(dt.timezone.utc)

    return parsed.isoformat().replace("+00:00", "Z")


def date_from_filename(rel_path: str) -> str | None:
    path = Path(rel_path)
    candidates = [path.stem, rel_path]
    for candidate in candidates:
        match = ISO_DATE_RE.search(candidate) or COMPACT_DATE_RE.search(candidate)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        try:
            return dt.datetime(year, month, day, tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def first_frontmatter_datetime(frontmatter: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for key in keys:
        normalized = normalize_datetime(frontmatter.get(key))
        if normalized:
            return normalized, key
    return None, None


def note_dates(frontmatter: dict[str, Any], rel_path: str, stat: os.stat_result) -> dict[str, str]:
    note_date, note_date_key = first_frontmatter_datetime(frontmatter, ("date",))
    created_at, created_key = first_frontmatter_datetime(
        frontmatter,
        ("created", "created_at", "created date", "creation date"),
    )
    modified_at, _ = first_frontmatter_datetime(
        frontmatter,
        ("modified", "modified_at", "updated", "updated_at", "lastmod"),
    )

    filename_date = date_from_filename(rel_path)
    file_created_at = iso_from_timestamp(getattr(stat, "st_birthtime", stat.st_ctime))
    file_modified_at = iso_from_timestamp(stat.st_mtime)

    timeline_at = note_date or created_at or filename_date or file_created_at or file_modified_at
    if note_date:
        date_source = f"frontmatter:{note_date_key}"
    elif created_at:
        date_source = f"frontmatter:{created_key}"
    elif filename_date:
        date_source = "filename"
    elif file_created_at:
        date_source = "file:created"
    else:
        date_source = "file:modified"

    return {
        "note_date": note_date or timeline_at,
        "created_at": created_at or file_created_at,
        "modified_at": modified_at or file_modified_at,
        "timeline_at": timeline_at,
        "date_source": date_source,
    }


def timeline_tags(timeline_at: str) -> list[str]:
    parsed = dt.datetime.fromisoformat(timeline_at.replace("Z", "+00:00"))
    return [
        f"year:{parsed:%Y}",
        f"month:{parsed:%Y-%m}",
        f"day:{parsed:%Y-%m-%d}",
    ]


def note_record(
    bank_id: str,
    vault_root: Path,
    source_path: Path,
    note_path: Path,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    vault_rel_path = note_path.relative_to(vault_root).as_posix()
    source_rel_path = note_path.relative_to(source_path).as_posix()
    document_id = (
        str(previous.get("document_id"))
        if previous and previous.get("document_id")
        else f"{bank_id}:{source_rel_path}"
    )
    raw_text = note_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(raw_text)
    stat = note_path.stat()
    dates = note_dates(frontmatter, vault_rel_path, stat)

    title = str(frontmatter.get("title") or note_path.stem)
    links = extract_wikilinks(raw_text)
    note_tags = sorted(set(extract_frontmatter_tags(frontmatter) + extract_hash_tags(body)))
    folder_parts = list(Path(vault_rel_path).parent.parts)
    folder_tags = [f"folder:{normalize_tag(part)}" for part in folder_parts if part != "."]
    tags = sorted(
        set(
            ["source:obsidian", f"vault:{normalize_tag(vault_root.name)}"]
            + folder_tags
            + [f"tag:{normalize_tag(tag)}" for tag in note_tags]
            + timeline_tags(dates["timeline_at"])
        )
    )

    metadata = {
        "source": "obsidian",
        "vault": vault_root.name,
        "vault_root": str(vault_root),
        "source_path": str(source_path),
        "absolute_path": str(note_path),
        "path": vault_rel_path,
        "vault_relative_path": vault_rel_path,
        "source_relative_path": source_rel_path,
        "title": title,
        "date": dates["note_date"],
        "created_at": dates["created_at"],
        "modified_at": dates["modified_at"],
        "timeline_at": dates["timeline_at"],
        "date_source": dates["date_source"],
        "links": json.dumps(links, ensure_ascii=True),
        "frontmatter_keys": json.dumps(sorted(frontmatter.keys()), ensure_ascii=True),
    }

    header = [
        f"Title: {title}",
        f"Path: {vault_rel_path}",
        f"Source Path: {source_path}",
        f"Date: {dates['note_date']}",
        f"Timeline Date: {dates['timeline_at']}",
        f"Created: {dates['created_at']}",
        f"Modified: {dates['modified_at']}",
        f"Date Source: {dates['date_source']}",
        f"Tags: {', '.join(note_tags) if note_tags else 'none'}",
        f"Links: {', '.join(links) if links else 'none'}",
        "",
    ]
    content = "\n".join(header) + raw_text
    sync_fingerprint = json.dumps(
        {"content": raw_text, "dates": dates},
        sort_keys=True,
        ensure_ascii=True,
    )

    return {
        "relative_path": source_rel_path,
        "vault_relative_path": vault_rel_path,
        "document_id": document_id,
        "hash": hashlib.sha256(sync_fingerprint.encode("utf-8")).hexdigest(),
        "mtime": stat.st_mtime,
        "dates": dates,
        "item": {
            "content": content,
            "context": "Obsidian note",
            "document_id": document_id,
            "timestamp": dates["timeline_at"],
            "metadata": metadata,
            "tags": tags,
        },
    }


def request_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("HINDSIGHT_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def retain_batch(
    api_url: str,
    bank_id: str,
    items: list[dict[str, Any]],
    retain_async: bool,
    timeout: float,
) -> dict[str, Any]:
    url = f"{api_url.rstrip('/')}/v1/default/banks/{quote(bank_id)}/memories"
    response = requests.post(
        url,
        headers=request_headers(),
        json={"items": items, "async": retain_async},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def delete_document(api_url: str, bank_id: str, document_id: str, timeout: float) -> None:
    url = (
        f"{api_url.rstrip('/')}/v1/default/banks/{quote(bank_id)}/documents/"
        f"{quote(document_id, safe='')}"
    )
    response = requests.delete(url, headers=request_headers(), timeout=timeout)
    if response.status_code == 404:
        return
    response.raise_for_status()


def batched(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def main() -> int:
    args = parse_args()
    source_arg = args.source if args.source is not None else args.vault
    vault_root = resolve_path(args.vault_root)
    source_path = resolve_source_path(vault_root, source_arg)
    manifest_path = resolve_path(args.manifest)

    if args.batch_size < 1:
        print("--batch-size must be at least 1", file=sys.stderr)
        return 2
    if not vault_root.exists() or not vault_root.is_dir():
        print(f"Vault root does not exist or is not a directory: {vault_root}", file=sys.stderr)
        return 2
    if not source_path.exists() or not source_path.is_dir():
        print(f"Source path does not exist or is not a directory: {source_path}", file=sys.stderr)
        return 2
    if not path_is_relative_to(source_path, vault_root):
        print(f"Source path must be inside vault root: {source_path}", file=sys.stderr)
        print(f"Vault root: {vault_root}", file=sys.stderr)
        return 2

    manifest = load_manifest(manifest_path)
    active_source = manifest_source(manifest, args.bank, vault_root, source_path)
    previous_notes: dict[str, Any] = active_source.get("notes", {})
    records = [
        note_record(
            bank_id=args.bank,
            vault_root=vault_root,
            source_path=source_path,
            note_path=path,
            previous=previous_notes.get(path.relative_to(source_path).as_posix()),
        )
        for path in iter_sync_files(source_path)
    ]
    current_by_path = {record["relative_path"]: record for record in records}

    changed = [
        record
        for record in records
        if args.force
        or previous_notes.get(record["relative_path"], {}).get("hash") != record["hash"]
    ]
    missing = [
        path
        for path, previous in previous_notes.items()
        if path not in current_by_path
        and isinstance(previous, dict)
        and str(previous.get("document_id", "")).startswith(f"{args.bank}:")
    ]

    delete_missing = bool(args.delete_missing)

    print(f"Vault root: {vault_root}")
    print(f"Source: {source_path}")
    print(f"Source relative path: {source_relative_path(vault_root, source_path)}")
    print(f"Bank: {args.bank}")
    print(f"Document IDs: {'legacy-preserved' if active_source.get('legacy_document_ids') else 'source-relative'}")
    print(f"Notes found: {len(records)}")
    print(f"Changed/new notes: {len(changed)}")
    print(f"Force retain: {args.force}")
    print(f"Missing notes in active source: {len(missing)}")
    print(f"Delete missing enabled: {delete_missing}")

    if args.dry_run:
        for record in changed[:20]:
            print(f"retain {record['document_id']}")
        if len(changed) > 20:
            print(f"... {len(changed) - 20} more retains")
        if delete_missing:
            for path in missing[:20]:
                print(f"delete {previous_notes[path]['document_id']}")
            if len(missing) > 20:
                print(f"... {len(missing) - 20} more deletes")
        return 0

    retained_count = 0
    for batch in batched(changed, args.batch_size):
        retain_batch(
            api_url=args.api_url,
            bank_id=args.bank,
            items=[record["item"] for record in batch],
            retain_async=not args.sync_mode,
            timeout=args.timeout,
        )
        retained_count += len(batch)
        print(f"Retained {retained_count}/{len(changed)} notes")

    deleted_count = 0
    if delete_missing:
        for path in missing:
            delete_document(
                api_url=args.api_url,
                bank_id=args.bank,
                document_id=previous_notes[path]["document_id"],
                timeout=args.timeout,
            )
            deleted_count += 1
            print(f"Deleted {deleted_count}/{len(missing)} documents")

    manifest["version"] = 2
    manifest["api_url"] = args.api_url
    manifest["updated_at"] = now_iso()
    active_source["updated_at"] = manifest["updated_at"]
    active_source["notes"] = {
        record["relative_path"]: {
            "document_id": record["document_id"],
            "hash": record["hash"],
            "mtime": record["mtime"],
            "vault_relative_path": record["vault_relative_path"],
            "timeline_at": record["dates"]["timeline_at"],
            "date_source": record["dates"]["date_source"],
            "synced_at": int(time.time()),
        }
        for record in records
    }
    save_manifest(manifest_path, manifest)

    print(f"Sync complete: retained={len(changed)} deleted={deleted_count}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
