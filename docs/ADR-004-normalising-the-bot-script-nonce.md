# ADR-004 — Normalising nhs.uk's per-response bot-script nonce before hashing

- **Status:** Accepted
- **Date:** 2026-10-26
- **Constrains:** ADR-001's "capture raw" — one narrow, recorded exception

## Context

Content-addressed storage made an unchanged practice cost zero bytes. From the
8 October 2026 capture it stopped working: `bytes_deduped` went from ~41 MB a
night to exactly 0, and every night stored the whole estate again
(42.85 MB/night mean over 18 nights, against 0.94 MB/night before). No commit
sits between 7 and 8 October.

The cause is in the pages. nhs.uk began embedding a bot-defence script:

```html
<script type="text/javascript"  src="/P_0Lo2xPwfy8/8uQWf1/…/NGe3A2ByoZ?v=29785b04-7285-92f2-fcc0-2312a5ff5f64" defer></script>
```

Its path and `?v=` GUID are regenerated on every response. On 22→23 October all
5,814 appointments pages changed hash; all 60 sampled were byte-identical once
that tag was masked. At that rate the pool (1.24 GB) would reach GitHub's 5 GB
repository limit around 21 January 2027.

## Decision

`snapshot.normalise()` replaces that one tag — matched by an exact, narrow
pattern, HTML only — with a fixed placeholder before the body is hashed and
stored. The manifest entry for a normalised record carries:

- `raw_sha256` — the hash of the bytes actually fetched, so what nhs.uk served
  is still attested;
- `normalised: ["bot-script-nonce"]` — which rule changed it.

Records the rule does not touch are stored byte-for-byte and carry neither field.

## Consequences

- Replayed against the 25 October capture: 6,403 of 6,404 pages matched the
  rule, and bytes written fall from 42.96 MB to 0.17 MB — the 7 pages still
  stored are genuine content changes.
- `parse_practice` gives identical results on raw and normalised bodies
  (checked on 1,500 real pages).
- The nonce itself is not kept. It is a per-request token with no meaning for
  availability, which is the reason this exception is acceptable.
- If nhs.uk changes the tag's shape, the pattern stops matching and storage
  falls back to raw bytes — growth returns, nothing is lost. `bytes_deduped`
  dropping to 0 in `run.json` is the signal to look again.
- Blobs already written since 8 October stay: the store is append-only. This
  stops the growth; it does not reclaim the ~0.77 GB already stored.
