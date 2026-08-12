# Prod deploy checklist — merging `reusable-contacts` → `main`

Everything that must happen on prod when this branch is rolled out. This branch
bundles several features that were built/tested together: the reusable-contacts
(multi-webinar membership) model, the Planning employee-count filter, the
Statistics Segments upgrades, and the CSV mojibake repair.

> Status legend: ⬜ pending · ✅ done

---

## 0. Before merge (working tree)

The branch has **uncommitted working-tree changes** beyond the committed
membership work — make sure they're all committed before merging:

- ⬜ Migration `065_add_bucket_stat_emp_range.py` (untracked)
- ⬜ Employee filter + Segments code (PlanningPage.tsx, SegmentsTab.tsx, statistics
  services/routers, schemas, db/models/outreach.py, api.ts)
- ⬜ Assign-flow follow-ups (same working tree): employee filter **prefills** from a
  segment's `stat_emp_min/max` on bucket select + a **clear ✕** button
  (PlanningPage.tsx); **50/50 fresh/reused** claim split + applied-filter summary
  appended to the list description (webinars.py `assign_bucket`)
- ⬜ CSV mojibake repair (`requirements.txt` +ftfy, `_helpers.py`, `uploads.py`,
  `scripts/fix_contact_mojibake.py`)
- ⬜ Copy Generator: removed Countries/Emp columns (`CopyGeneratorPage.tsx`)
- ⬜ Segments drilldown heatmap + filter-aware assign Total/Remaining + Good
  Available header stats (`SegmentsTab.tsx`, `PlanningPage.tsx`, `buckets.py`,
  `api.ts`, `_helpers.py`)
- ⬜ **Booking-attribution DATA layer** (per-webinar booking history, capture-forward):
  `db/models/ghl.py` (`WebinarBookingAttribution`), migration `066`,
  `services/ghl_appointments.py` (`attribute_booking`), `services/booking_attribution.py`,
  `services/ghl_sync.py` (capture-forward hook), `scripts/backfill_booking_attribution.py`,
  + docstring/dead-code/A/B-note cleanups in `services/ghl_statistics_source.py`.
  **The stats dashboards do NOT read it yet — that's §6.**

Already committed on the branch: `064_add_webinar_contact_memberships.py`,
`scripts/backfill_reusable_contacts.py`.

---

## 1. Dependencies

- ⬜ **`ftfy==6.3.1`** added to `requirements.txt`. Render runs
  `pip install -r requirements.txt` on deploy, so this is automatic — just
  confirm the build picked it up (mojibake repair silently degrades without it:
  double-encoded CJK names won't fully recover).

## 2. Database migrations — `alembic upgrade head`

Applies, in order:

- ⬜ `063_weekly_report_settings` — **no-op** on prod (the `report_settings`
  table was pre-created via idempotent DDL). Included because prod's
  alembic_version predates it.
- ⬜ `064_add_webinar_contact_memberships` — creates the
  `webinar_contact_memberships` junction + `contacts` cache columns
  (`assigned_membership_count`, `times_invited`, `last_invited_at`).
- ⬜ `065_add_bucket_stat_emp_range` — additive nullable `stat_emp_min` /
  `stat_emp_max` on `outreach_buckets` (fast, metadata-only).
- ⬜ `066_add_webinar_booking_attribution` — new `webinar_booking_attribution`
  table (per-booking → webinar attribution that survives GHL overwriting its
  single opportunity). Additive (new table only), safe on current prod.

## 3. Data backfills / scripts (run **after** migrations, in this order)

- ⬜ **Reusable-contacts backfill** — `python -m scripts.backfill_reusable_contacts`
  Idempotent, keyset-chunked (under the 120s statement_timeout), builds
  `ix_contacts_claimable` **and** `ix_contacts_good_avail` (the latter makes the
  Planning "Good Available" header aggregate an index-only scan) CONCURRENTLY. The
  index step now lifts `statement_timeout` for its DDL session and drops+rebuilds
  an INVALID index left by any prior killed build, so a re-run recovers instead of
  silently skipping. Until it runs, reuse counts read as empty.
  - ⬜ After it finishes, run **`VACUUM (ANALYZE) contacts`** — the backfill's bulk
    cache writes leave the visibility map stale, and `ix_contacts_good_avail` only
    delivers its index-only scan (verified via `EXPLAIN`) once the VM is all-visible;
    ANALYZE also refreshes stats so the planner picks the new indexes.
- ⬜ **Mojibake repair** — `python -m scripts.fix_contact_mojibake --dry-run`
  then rerun without the flag.
  *(Being run NOW on prod ahead of the merge — see §5. Only re-run at merge time
  to catch any newly-imported bad rows in the interim.)*
- ⬜ **Booking attribution backfill** — `python -m scripts.backfill_booking_attribution --dry-run`
  (review the attribution-source mix on the ~559 multi-booking contacts), then
  rerun without the flag. **Must run AFTER 064 + 066** (needs both the membership
  table and `webinar_booking_attribution`). Idempotent; never re-attributes a
  capture-forward-locked row's *webinar* (GHL overwrites the opp's source number),
  but the *outcome* columns — won / disqualified / lead_quality / call_status —
  always refresh from the current opp, so a deal that closes weeks after the
  booking is recorded. Going forward, the GHL sync captures each new booking's
  webinar automatically (locked). NOTE: the stats dashboards don't read this table
  yet — that's the follow-up refactor (§6), so this backfill just populates the
  data ahead of it; it doesn't change any displayed number.

No stats recompute is required for the Segments/employee work — the per-segment
funnel is served by a live endpoint, not a precomputed snapshot. (The booking-
attribution refactor WILL need a recompute — see §6.)

## 4. Config

- ⬜ **Weekly report** — paste the Resend API key at `/connectors/resend` if not
  already set (settings row is enabled: Wed 14:00 America/Chicago). Independent of
  this merge but still outstanding on prod; test with `POST /reports/send-test`.

## 5. Done ahead of merge (no code push needed)

- ✅ **Mojibake backfill run on prod — 2026-08-08, ~24,215 rows repaired**
  (21,714 first pass + 2,501 tail resume via `--start-after`). Repaired
  already-imported double-encoded names (verified: `kim@sfkorean.com` → `김진형`,
  the contact from the original screenshot). Ran the local branch's
  `scripts/fix_contact_mojibake.py` (needs no deployed code) against the prod
  pooler. Idempotent, so a re-run at merge time only touches rows imported in the
  interim.

## 6. Verification after deploy

- ⬜ Reusable-contacts e2e (already verified locally + on a read-only prod sample):
  assign → mark-used → reuse filter → release; bucket REMAINING = fresh baseline;
  `GET /outreach/buckets/eligible` reuse-aware remaining.
- ⬜ Planning: employee-count min/max filter under the country filter narrows the
  assignable pool (literal `employee_count BETWEEN`; unknown-size contacts excluded
  when a range is set).
- ⬜ Statistics → Segments: quality column, per-segment employee min/max editor
  persists (065 cols) via `PUT /outreach/buckets/{id}`, click-to-expand drilldown
  + "Suggested X–Y" chip.
- ⬜ Spot-check a previously-mangled contact now shows the correct name.
- ⬜ Booking-attribution backfill dry-run source mix looks sane (most bookings
  resolve via `source_number`/`attended`/`invited`, not `unknown`); GHL sync
  logs no capture-forward errors.

## 7. FOLLOW-UP PR — booking-attribution stats consumption (validate vs real data)

This push ships only the DATA layer (captures + backfills per-webinar booking
attribution). Making the dashboards READ it is a separate PR, done after the
backfill so it can be validated against real numbers. Scope:
- Rewrite the sales funnel to source from `webinar_booking_attribution WHERE
  webinar_id=:wid` instead of the collapsed opportunity: per-list Batch C
  (`_compute_per_list_metrics`), by-source (`_compute_per_source_cells`),
  by-employee (`_compute_per_employee_cells`), nonjoiner cohort; count
  shows/no-shows/confirmed/canceled from `call_status`, won/disqualified/lead_quality
  from the snapshot, calls-passed from `call_at`.
- **`uniqueBookers`** (COUNT DISTINCT contact) becomes the displayed "Bookings"
  (KPI tile + funnel column + Segments/source/employee "bookings" columns); keep
  `totalBookings` for the ratio denominators + as the modal's "all" reference.
- **Bookings modal**: headline unique bookers, show total opportunities as
  reference (add `unique_total` to `ContactDrilldownResponse`;
  `build_webinar_wide_opp_query` count_sql gets a `COUNT(DISTINCT ghl_contact_id)`).
- **Non-member-attributed bookings** (booking whose contact isn't on a list for
  the attributed webinar) count in the webinar TOTAL under a "No list" bucket
  (compute the webinar summary sales metrics from attribution WITHOUT the
  membership join, so the headline isn't under-counted by the per-list sum).
- Then **`POST /statistics/recompute`** and diff aggregate bookings before/after
  (total should only redistribute across webinars, not change wildly).
- Retire the deprecated number-only `_compute_webinar_metrics`/`_compute_webinar_summary`.

---

## Excluded (already live on `main` — do NOT re-run for this merge)

- Blocklist flag: migration 060 + `backfill_contact_blocklist` (deployed).
- Firmographics backfill + `rebucket_employee_range` (ran on main 2026-07-30/31).
