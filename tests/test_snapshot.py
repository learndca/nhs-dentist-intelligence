"""Tests for the append-only snapshot store and crawl health checks.

These guard the one property the project cannot recover from losing: that a
night's capture, once written, is complete and never silently overwritten.
"""

from __future__ import annotations

import gzip
import json

import pytest
from health import check_run
from snapshot import (
    Record,
    SnapshotWriter,
    available_days,
    iter_payloads,
    latest_day,
    read_blob,
    read_manifest,
    read_run,
)


def make_record(i: int, body: bytes = b"<html>practice</html>", status: int = 200) -> Record:
    return Record(
        practice_id=f"P{i:05d}",
        url=f"https://example.test/practice/{i}",
        fetched_at="2026-07-25T03:00:00+00:00",
        status=status,
        body=body,
        content_type="text/html",
    )


def write_day(root, day: str, count: int, *, body_prefix: bytes = b"x", failures: int = 0):
    with SnapshotWriter(root=root, day=day, route="test") as w:
        for i in range(count):
            w.add(make_record(i, body=body_prefix + str(i).encode()))
        for i in range(failures):
            w.add(make_record(9000 + i, body=b"", status=503))
    return w


def test_roundtrip_preserves_raw_bytes(tmp_path):
    body = b"<html>\xc2\xa3 caf\xc3\xa9 \x00 binary-ish</html>"
    with SnapshotWriter(root=tmp_path, day="2026-07-25", route="html") as w:
        digest = w.add(make_record(1, body=body))

    assert read_blob(tmp_path, digest) == body
    entries = list(read_manifest(tmp_path, "2026-07-25"))
    assert len(entries) == 1
    assert entries[0]["practice_id"] == "P00001"
    assert entries[0]["bytes"] == len(body)


def test_identical_bodies_are_stored_once(tmp_path):
    """The point of content addressing: an unchanged practice costs no new bytes."""
    same = b"unchanged payload"
    with SnapshotWriter(root=tmp_path, day="2026-07-25") as w:
        for i in range(5):
            w.add(make_record(i, body=same))

    blobs = list((tmp_path / "blobs").rglob("*.gz"))
    assert len(blobs) == 1
    assert w.written == 5
    assert w.bytes_new > 0
    assert w.bytes_deduped > 0


def test_dedup_persists_across_nights(tmp_path):
    body = b"stable practice record"
    with SnapshotWriter(root=tmp_path, day="2026-07-25") as first:
        first.add(make_record(1, body=body))
    with SnapshotWriter(root=tmp_path, day="2026-07-26") as second:
        second.add(make_record(1, body=body))

    assert first.bytes_new > 0
    assert second.bytes_new == 0, "second night re-stored an unchanged payload"
    assert len(list((tmp_path / "blobs").rglob("*.gz"))) == 1
    # Both nights still present a complete view of the estate.
    assert len(list(read_manifest(tmp_path, "2026-07-26"))) == 1


def test_completed_day_cannot_be_overwritten(tmp_path):
    write_day(tmp_path, "2026-07-25", 3)
    with pytest.raises(FileExistsError, match="refusing to overwrite history"), SnapshotWriter(
        root=tmp_path, day="2026-07-25"
    ):
        pass


def test_aborted_day_can_be_retried(tmp_path):
    """A crashed run must not lock the day out — that would cost a night of history."""
    with pytest.raises(RuntimeError), SnapshotWriter(root=tmp_path, day="2026-07-25") as w:
        w.add(make_record(1))
        raise RuntimeError("network died")

    assert read_run(tmp_path, "2026-07-25")["complete"] is False
    with SnapshotWriter(root=tmp_path, day="2026-07-25") as w:
        w.add(make_record(1))
    assert read_run(tmp_path, "2026-07-25")["complete"] is True


def test_failures_are_recorded_not_swallowed(tmp_path):
    with SnapshotWriter(root=tmp_path, day="2026-07-25") as w:
        w.add(make_record(1))
        w.add(make_record(2, body=b"", status=503))

    run = read_run(tmp_path, "2026-07-25")
    assert run["records_ok"] == 1
    assert run["records_failed"] == 1
    assert run["error_sample"][0]["status"] == 503
    # The failed practice still appears in the manifest, with no blob.
    entries = list(read_manifest(tmp_path, "2026-07-25"))
    assert len(entries) == 2
    assert entries[1]["sha256"] == ""


def test_iter_payloads_yields_only_successes(tmp_path):
    write_day(tmp_path, "2026-07-25", 4, failures=2)
    payloads = list(iter_payloads(tmp_path, "2026-07-25"))
    assert len(payloads) == 4
    assert all(body for _, body in payloads)


def test_day_discovery(tmp_path):
    write_day(tmp_path, "2026-07-25", 2)
    write_day(tmp_path, "2026-07-26", 2)
    assert available_days(tmp_path) == ["2026-07-25", "2026-07-26"]
    assert latest_day(tmp_path) == "2026-07-26"
    assert available_days(tmp_path / "nope") == []


def test_manifest_is_deterministic_gzip(tmp_path):
    """mtime=0 keeps an unchanged manifest byte-identical so git does not churn."""
    write_day(tmp_path, "2026-07-25", 3)
    raw = (tmp_path / "2026-07-25" / "manifest.jsonl.gz").read_bytes()
    assert gzip.decompress(raw).count(b"\n") == 3
    assert raw[4:8] == b"\x00\x00\x00\x00", "gzip header carries a timestamp"


# -- health checks ---------------------------------------------------------


def test_health_passes_on_first_healthy_capture(tmp_path):
    write_day(tmp_path, "2026-07-25", 9000)
    checks = check_run(tmp_path)
    assert all(c.passed for c in checks), [str(c) for c in checks]


def test_health_flags_volume_collapse(tmp_path):
    write_day(tmp_path, "2026-07-25", 9000)
    write_day(tmp_path, "2026-07-26", 6000)
    failed = [c for c in check_run(tmp_path) if not c.passed]
    assert any(c.name == "volume vs prior run" for c in failed)


def test_health_flags_implausibly_small_capture(tmp_path):
    write_day(tmp_path, "2026-07-25", 100)
    failed = [c for c in check_run(tmp_path) if not c.passed]
    assert any(c.name == "plausible volume" for c in failed)


def test_health_flags_high_failure_rate(tmp_path):
    write_day(tmp_path, "2026-07-25", 9000, failures=1000)
    failed = [c for c in check_run(tmp_path) if not c.passed]
    assert any(c.name == "failure rate" for c in failed)


def test_health_tolerates_normal_drift(tmp_path):
    write_day(tmp_path, "2026-07-25", 9000)
    write_day(tmp_path, "2026-07-26", 9050)
    assert all(c.passed for c in check_run(tmp_path))


def test_health_reports_missing_store(tmp_path):
    checks = check_run(tmp_path / "empty")
    assert not checks[0].passed


def test_run_metadata_is_valid_json(tmp_path):
    write_day(tmp_path, "2026-07-25", 2)
    text = (tmp_path / "2026-07-25" / "run.json").read_text()
    assert json.loads(text)["route"] == "test"


# -- bot-script nonce (ADR-004) ---------------------------------------------

def _page(path: bytes, guid: bytes, text: bytes = b"Taking new patients") -> bytes:
    return (
        b"<html><body>" + text + b'<script type="text/javascript"  src="/'
        + path + b"?v=" + guid + b'" defer></script></body></html>'
    )


NIGHT_A = _page(b"SdiV/YaAS/cLN/zrY/lI0w/rfm7QViup91O", b"0ef659d1-8552-69b1-1cb4-ee51a8ce0212")
NIGHT_B = _page(b"P_0Lo2xPwfy8/8uQWf1/7RWIAQ/NkYawL7O7Y", b"29785b04-7285-92f2-fcc0-2312a5ff5f64")


def test_nonce_only_change_dedups_across_nights(tmp_path):
    """The 8 October regression: same page, new nonce, must cost no new bytes."""
    with SnapshotWriter(root=tmp_path, day="2026-10-24", route="html") as w:
        a = w.add(make_record(1, body=NIGHT_A))
    with SnapshotWriter(root=tmp_path, day="2026-10-25", route="html") as w:
        b = w.add(make_record(1, body=NIGHT_B))
    assert a == b
    assert w.bytes_new == 0 and w.bytes_deduped > 0


def test_real_content_change_is_still_stored(tmp_path):
    with SnapshotWriter(root=tmp_path, day="2026-10-24", route="html") as w:
        a = w.add(make_record(1, body=NIGHT_A))
        b = w.add(make_record(2, body=_page(b"x/y", b"29785b04-7285-92f2-fcc0-2312a5ff5f64", b"Not taking")))
    assert a != b


def test_manifest_attests_raw_bytes_when_normalised(tmp_path):
    import hashlib

    with SnapshotWriter(root=tmp_path, day="2026-10-25", route="html") as w:
        digest = w.add(make_record(1, body=NIGHT_B))
    entry = next(read_manifest(tmp_path, "2026-10-25"))
    assert entry["normalised"] == ["bot-script-nonce"]
    assert entry["raw_sha256"] == hashlib.sha256(NIGHT_B).hexdigest()
    assert entry["bytes"] == len(NIGHT_B)
    stored = read_blob(tmp_path, digest)
    assert b"__nonce__" in stored and b"Taking new patients" in stored


def test_untouched_bodies_carry_no_normalisation_fields(tmp_path):
    with SnapshotWriter(root=tmp_path, day="2026-10-25", route="html") as w:
        digest = w.add(make_record(1, body=b"<html>plain</html>"))
    entry = next(read_manifest(tmp_path, "2026-10-25"))
    assert "raw_sha256" not in entry and "normalised" not in entry
    assert read_blob(tmp_path, digest) == b"<html>plain</html>"


def test_non_html_is_never_normalised(tmp_path):
    rec = make_record(1, body=NIGHT_B)
    rec.content_type = "application/xml"
    with SnapshotWriter(root=tmp_path, day="2026-10-25", route="html") as w:
        digest = w.add(rec)
    assert read_blob(tmp_path, digest) == NIGHT_B
