# Deploy: 6-webinar non-joiner definition

Ships `services/nonjoiners.py` as the single source of truth for the non-joiner pool,
replacing the Statistics page's previous-webinar-only derivation. See the **Non-joiners**
section of [statistics-formulas.md](statistics-formulas.md) for the rule itself.

**No migration.** No schema change, no new columns, no backfill script.
`webinars.nonjoiner_source_webinar_id` is kept and reused — it now defines the chain order
rather than the pool source.

## Required post-deploy step

**Recompute all statistics snapshots** — `POST /statistics/recompute` (or the Statistics
page's "Recompute now").

Snapshots hold frozen per-webinar payloads. Every snapshot written before this deploy
carries the old previous-webinar-only Nonjoiners row (W152: 2,041 pool / 790 regs instead
of 7,502 / 966). Until they are rebuilt:

- the Statistics page serves stale Nonjoiners rows from the snapshot rather than computing
  the new pool;
- the per-webinar report's **baseline columns** are snapshot-derived
  (`_snapshot_counts` over `prior_primary[:BASELINE_WINDOW]`), so a freshly generated
  report would compare a new-definition current webinar against old-definition baselines;
- `scorecard.nonjoinerRegs` falls back to the snapshot value when the live cohort count is
  zero.

Regenerating per-webinar reports after the recompute is optional but recommended for any
report whose numbers get quoted — reports are frozen payloads too.

## What is NOT a bug after this deploy

- **Nonjoiners row sales columns read ~0.** The pool excludes converted contacts (booked /
  won / disqualified), so by construction the cohort holds almost no opportunities
  (W152: 53 → 5). This is the agreed one-pool rule — we report on exactly who we invited.
  Judge non-joiner sends on registrations and attendance.
- **NJ regs read slightly below the historical operational figure.** W152 reads 966 vs the
  974 in the Lloyd report; the 8 difference is contacts who were mailed but should have
  been suppressed.
- **NO LIST DATA drops.** W152: 477 → 301. Non-joiners are now correctly carved out of the
  leftover pool instead of hiding in it. The Total row still reconciles:
  `2,711 = 1,444 planned + 966 non-joiners + 301 no-list-data`.

## Behaviour worth knowing

`schedule_recompute_for_broadcast` now also recomputes the **next 6 webinars** after the
one whose broadcast was synced, because a broadcast feeds the non-joiner pool of every
webinar in its forward window. Re-syncing W150 recomputes W150–W156. This makes WebinarGeek
syncs somewhat heavier than before.

The pool query costs ~23s against prod from outside the datacenter (~6s before the
converted-contact suppression), well inside the 120s statement cap. Watch it if more
suppression rules get added.
