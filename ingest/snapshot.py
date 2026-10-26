"""Append-only, content-addressed snapshot store (ledger step 1.2).

Capture raw, parse later: yesterday cannot be re-fetched, but it can always be
re-parsed. Nothing here interprets a payload — it only stores bytes durably and
lets a later parser walk any night's capture.

Layout::

    data/snapshots/
      blobs/ab/abcdef...gz          content-addressed raw payload, gzipped
      2026-07-25/manifest.jsonl.gz  one JSON line per practice for that night
      2026-07-25/run.json           run-level metadata and counts

Why content-addressed rather than one file per practice per night: most practices
do not change from one night to the next, so storing by content hash means an
unchanged practice costs zero additional bytes. A night's manifest still lists
every practice, so each capture remains a complete, independently parseable view
of the estate.

One exception to byte-for-byte storage, recorded in ADR-004: from 8 October 2026
nhs.uk began embedding a bot-defence ``<script>`` whose path and ``?v=`` GUID are
regenerated on every response. Hashing raw bytes then matched nothing, and every
night stored the whole estate again (~43 MB instead of ~1 MB). ``normalise``
replaces that one tag with a fixed placeholder before hashing and storing; the
manifest keeps ``raw_sha256`` of the bytes actually fetched and lists the rule in
``normalised``. Nothing else in a payload is ever rewritten.

The store is append-only by contract. Blobs are immutable once written and a
completed day is never rewritten — see ``guard_day_not_final``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "snapshots"


# A per-response nonce, not content: random path segments and a fresh GUID on
# every fetch of an otherwise identical page. Kept deliberately narrow — if the
# tag's shape changes, nothing matches and storage simply falls back to raw.
_BOT_SCRIPT = re.compile(
    rb'<script type="text/javascript"\s+src="/[A-Za-z0-9_\-/]+'
    rb'\?v=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"\s+defer></script>'
)
_BOT_SCRIPT_PLACEHOLDER = b'<script type="text/javascript" src="/__nonce__" defer></script>'


def normalise(body: bytes, content_type: str = "") -> tuple[bytes, list[str]]:
    """Strip per-response noise so an unchanged page hashes the same every night.

    Returns the body to store and the names of the rules that changed it (empty
    when the body is stored exactly as fetched).
    """
    if "html" not in content_type.lower():
        return body, []
    out, n = _BOT_SCRIPT.subn(_BOT_SCRIPT_PLACEHOLDER, body)
    return (out, ["bot-script-nonce"]) if n else (body, [])


def utc_today() -> str:
    """Capture date, UTC. The nightly cron runs ~03:00 UTC, so UTC avoids a
    BST/GMT boundary silently producing two captures for one calendar day."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass(slots=True)
class Record:
    """One fetched practice payload, before any parsing."""

    practice_id: str
    url: str
    fetched_at: str
    status: int
    body: bytes
    content_type: str = ""
    error: str = ""
    # Free-form provenance: which route produced this (api / html), page number, etc.
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.body) and not self.error


class SnapshotWriter:
    """Writes one night's capture. Use as a context manager.

    >>> with SnapshotWriter(route="html") as w:      # doctest: +SKIP
    ...     w.add(record)
    """

    def __init__(
        self,
        root: Path | str = DEFAULT_ROOT,
        day: str | None = None,
        route: str = "unknown",
        resume: bool = False,
    ) -> None:
        self.root = Path(root)
        self.day = day or utc_today()
        self.route = route
        self.resume = resume
        self.day_dir = self.root / self.day
        self.blob_dir = self.root / "blobs"
        self._manifest_path = self.day_dir / "manifest.jsonl.gz"
        self._run_path = self.day_dir / "run.json"
        self._fh: gzip.GzipFile | None = None
        self._started = datetime.now(UTC)

        self.written = 0
        self.failed = 0
        self.bytes_new = 0
        self.bytes_deduped = 0
        self._errors: list[dict] = []
        # Practice IDs already captured today, populated on resume.
        self.already_captured: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Self:
        guard_day_not_final(self.root, self.day)
        self.day_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)

        # A full-estate capture takes hours. If it dies at practice 5,000 we
        # resume rather than re-fetch — both to save the source's bandwidth and
        # because a restart-from-zero may not finish before the next run is due.
        append = self.resume and self._manifest_path.exists()
        if append:
            self._load_prior_progress()
        # mtime=0 so an unchanged manifest is byte-identical and does not churn git.
        # gzip members concatenate cleanly; the reader walks them transparently.
        self._fh = gzip.GzipFile(self._manifest_path, "ab" if append else "wb", mtime=0)
        return self

    def _load_prior_progress(self) -> None:
        """Recover counts and captured IDs from a partial run so run.json stays
        truthful about the day as a whole, not just this process's share."""
        try:
            for entry in read_manifest(self.root, self.day):
                self.already_captured.add(entry["practice_id"])
                if entry.get("sha256"):
                    self.written += 1
                else:
                    self.failed += 1
        except (OSError, EOFError, json.JSONDecodeError):
            # A truncated final member means the process was killed mid-write.
            # Whatever we recovered is still valid; the rest gets re-fetched.
            pass

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._write_run_metadata(aborted=exc_type is not None, error=repr(exc) if exc else "")

    # -- writing -----------------------------------------------------------

    def add(self, record: Record) -> str:
        """Store one record. Returns the blob digest (empty string on failure)."""
        if self._fh is None:
            raise RuntimeError("SnapshotWriter used outside its context manager")

        digest = ""
        applied: list[str] = []
        if record.ok:
            stored, applied = normalise(record.body, record.content_type)
            digest, is_new, size = self._put_blob(stored)
            self.written += 1
            if is_new:
                self.bytes_new += size
            else:
                self.bytes_deduped += size
        else:
            self.failed += 1
            if len(self._errors) < 50:  # a sample is enough to diagnose; don't bloat run.json
                self._errors.append(
                    {
                        "practice_id": record.practice_id,
                        "url": record.url,
                        "status": record.status,
                        "error": record.error[:300],
                    }
                )

        entry = {
            "practice_id": record.practice_id,
            "url": record.url,
            "fetched_at": record.fetched_at,
            "status": record.status,
            "content_type": record.content_type,
            "sha256": digest,
            "bytes": len(record.body),
        }
        if applied:
            entry["raw_sha256"] = hashlib.sha256(record.body).hexdigest()
            entry["normalised"] = applied
        if record.error:
            entry["error"] = record.error[:300]
        if record.meta:
            entry["meta"] = record.meta

        self._fh.write((json.dumps(entry, sort_keys=True) + "\n").encode())
        return digest

    def _put_blob(self, body: bytes) -> tuple[str, bool, int]:
        digest = hashlib.sha256(body).hexdigest()
        path = self.blob_dir / digest[:2] / f"{digest}.gz"
        if path.exists():
            return digest, False, path.stat().st_size

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = gzip.compress(body, compresslevel=6, mtime=0)
        # Write-then-rename: a killed process must never leave a truncated blob
        # sitting at a name that claims to be that hash.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest, True, len(payload)

    def _write_run_metadata(self, *, aborted: bool, error: str) -> None:
        finished = datetime.now(UTC)
        meta = {
            "day": self.day,
            "route": self.route,
            "started_at": self._started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round((finished - self._started).total_seconds(), 1),
            "records_ok": self.written,
            "records_failed": self.failed,
            "bytes_new": self.bytes_new,
            "bytes_deduped": self.bytes_deduped,
            "aborted": aborted,
            "complete": not aborted,
            "error_sample": self._errors,
        }
        if error:
            meta["error"] = error
        self._run_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


# -- reading ---------------------------------------------------------------


def guard_day_not_final(root: Path | str, day: str) -> None:
    """Refuse to overwrite a completed capture.

    The store's whole value is that history is never lost. A re-run against a day
    that already completed would silently replace real history with a second,
    later view of it.
    """
    run = Path(root) / day / "run.json"
    if run.exists():
        meta = json.loads(run.read_text())
        if meta.get("complete"):
            raise FileExistsError(
                f"snapshot for {day} already completed "
                f"({meta.get('records_ok')} records) — refusing to overwrite history"
            )


def available_days(root: Path | str = DEFAULT_ROOT) -> list[str]:
    """Capture dates present in the store, oldest first."""
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and p.name != "blobs" and (p / "run.json").exists()
    )


def latest_day(root: Path | str = DEFAULT_ROOT) -> str | None:
    days = available_days(root)
    return days[-1] if days else None


def read_run(root: Path | str, day: str) -> dict:
    return json.loads((Path(root) / day / "run.json").read_text())


def read_manifest(root: Path | str, day: str) -> Iterator[dict]:
    """Yield every manifest entry for a day, in capture order."""
    path = Path(root) / day / "manifest.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_blob(root: Path | str, digest: str) -> bytes:
    """Resolve a content hash back to the stored payload: the raw bytes, except
    where the manifest entry lists ``normalised`` rules (see ``normalise``)."""
    path = Path(root) / "blobs" / digest[:2] / f"{digest}.gz"
    with gzip.open(path, "rb") as fh:
        return fh.read()


def iter_payloads(root: Path | str, day: str) -> Iterator[tuple[dict, bytes]]:
    """Yield ``(manifest_entry, raw_body)`` for every successful record of a day.

    This is the entry point every future parser should use — it is the reason
    capture and parsing could be separated in the first place.
    """
    for entry in read_manifest(root, day):
        if entry.get("sha256"):
            yield entry, read_blob(root, entry["sha256"])


def to_record(entry: dict, body: bytes) -> Record:
    return Record(
        practice_id=entry["practice_id"],
        url=entry["url"],
        fetched_at=entry["fetched_at"],
        status=entry["status"],
        body=body,
        content_type=entry.get("content_type", ""),
        meta=entry.get("meta", {}),
    )


def as_dict(record: Record) -> dict:
    d = asdict(record)
    d.pop("body", None)
    return d
