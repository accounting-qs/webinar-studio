# Statistics Formulas — Handoff Reference

This document explains how every metric in the Statistics page is sourced, derived, and aggregated. It serves as the handoff reference for the next engineer or AI model.

## Workbook Shape

- **Sheet**: Webinar Funnel (single sheet)
- **Columns**: A through CK (89 columns). BD–CK are scratch/unused.
- **Rows**: Flat data, no merged cells
- **Parent rows**: Detected by numeric value in column A (webinar number 106–136)
- **Child rows**: Belong to the parent above them until the next parent row

### Parent Row Variants

| Variant | Webinars | Column F value |
|---------|----------|----------------|
| Legacy | 106–121 | `TOTAL` |
| Titled | 122–133 | Title text (e.g. `TITLE: Scale Your Business`) |
| Bare | 134–136 | Blank or minimal |

### Included Row Ranges

- Safe window: rows 2–305
- Webinar 136 (starts row 301): include child rows 302–305 only
- **Excluded**: rows 306–366 (scratch notes, capacity planning, orphan ideas)

## Column Mapping (Excel Letter → API Key)

| Col | API Key | Col | API Key |
|-----|---------|-----|---------|
| A | webinarNumber | B | dateOrNote |
| C | status | D | listUrl |
| E | description | F | listNameOrTitle |
| G | sendInfo | H | descLabel |
| I | titleText | J | listSize |
| K | listRemain | L | gcalInvited |
| M | accountsNeeded | N | createdDate |
| O | industry | P | employeeRange |
| Q | country | R | invited |
| S | unsubscribes | T | ghlPageViews |
| U | lpRegs | V | yesMarked |
| W | yesAttended | X | yes10MinPlus |
| Y | yesAttendBySmsClick | Z | yesBookings |
| AA | maybeMarked | AB | maybeAttended |
| AC | maybe10MinPlus | AD | maybeAttendBySmsClick |
| AE | maybeBookings | AF | selfRegMarked |
| AG | selfRegAttended | AH | selfReg10MinPlus |
| AI | selfRegBookings | AJ | totalRegs |
| AK | totalAttended | AL | attendBySmsReminder |
| AM | total10MinPlus | AN | total30MinPlus |
| AO | totalBookings | AP | totalCallsDatePassed |
| AQ | confirmed | AR | shows |
| AS | noShows | AT | canceled |
| AU | won | AV | disqualified |
| AW | qualified | AX | leadQualityGreat |
| AY | leadQualityOk | AZ | leadQualityBarelyPassable |
| BA | leadQualityBadDq | BB | avgProjectedDealSize |
| BC | avgClosedDealValue | | |

## Source-Fed Fields

These fields come directly from the workbook (v1) or will come from GoHighLevel (v2). They are **not** computed in application code.

`status`, `listUrl`, `description`, `sendInfo`, `descLabel`, `titleText`, `listSize`, `listRemain`, `gcalInvited`, `accountsNeeded`, `createdDate`, `industry`, `employeeRange`, `country`, `invited`, `unsubscribes`, `ghlPageViews`, `lpRegs`, `yesMarked`, `yesAttended`, `yes10MinPlus`, `yesAttendBySmsClick`, `yesBookings`, `maybeMarked`, `maybeAttended`, `maybe10MinPlus`, `maybeAttendBySmsClick`, `maybeBookings`, `selfRegMarked`, `selfRegAttended`, `selfReg10MinPlus`, `selfRegBookings`, `totalRegs`, `totalAttended`, `attendBySmsReminder`, `total10MinPlus`, `total30MinPlus`, `totalBookings`, `totalCallsDatePassed`, `confirmed`, `shows`, `noShows`, `canceled`, `won`, `disqualified`, `qualified`, `leadQualityGreat`, `leadQualityOk`, `leadQualityBarelyPassable`, `leadQualityBadDq`, `avgProjectedDealSize`, `avgClosedDealValue`

### Notes on `accountsNeeded`

Source-fed from workbook values in v1 because the sheet uses mixed logic including `/300/7`, `/300/5`, `/100/5`, literals, and parent sums. Do not globally recompute.

## Derived Fields

All derived fields are computed in application code (`services/statistics.py`), not copied from workbook cells (which contain broken formulas in later rows).

### Zero-Safe Rule

All division operations return `null` when the denominator is zero or null. The frontend displays `null` as `—` (em dash).

### Delivery

| Field | Formula |
|-------|---------|
| `unsubPercent` | `unsubscribes / invited` |
| `ctrPercent` | `ghlPageViews / invited` |
| `lpRegPercent` | `lpRegs / ghlPageViews` |

### Yes

| Field | Formula |
|-------|---------|
| `yesPer1kInv` | `yesMarked / (invited / 1000)` |
| `yesPercent` | `yesMarked / invited` |
| `yesAttendPercent` | `yesAttended / yesMarked` |
| `yesStay10MinPercent` | `yes10MinPlus / yesAttended` |
| `yesAttendBySmsClickPercent` | `yesAttendBySmsClick / yesAttended` (zero-safe) |
| `yesBookingsPer1kInv` | `yesBookings / (invited / 1000)` |

### Maybe

| Field | Formula |
|-------|---------|
| `maybePer1kInv` | `maybeMarked / (invited / 1000)` |
| `maybeAttendPercent` | `maybeAttended / maybeMarked` |
| `maybeStay10MinPercent` | `maybe10MinPlus / maybeAttended` |
| `maybeAttendBySmsClickPercent` | `maybeAttendBySmsClick / maybeAttended` (zero-safe) |
| `maybeBookingsPer1kInv` | `maybeBookings / (invited / 1000)` |

### Self Reg

| Field | Formula |
|-------|---------|
| `selfRegPer1kInv` | `selfRegMarked / (invited / 1000)` |
| `selfRegAttendPercent` | `selfRegAttended / selfRegMarked` |
| `selfRegStay10MinPercent` | `selfReg10MinPlus / selfRegAttended` |
| `selfRegBookingsPer1kInv` | `selfRegBookings / (invited / 1000)` |

### Attendance

| Field | Formula |
|-------|---------|
| `invitedToRegPercent` | `totalRegs / invited` |
| `regToAttendPercent` | `totalAttended / totalRegs` |
| `invitedToAttendPercent` | `totalAttended / invited` |
| `totalAttendedPer1kInv` | `totalAttended / (invited / 1000)` |
| `attendBySmsReminderPercent` | `attendBySmsReminder / totalAttended` |
| `total10MinPlusPer1kInv` | `total10MinPlus / (invited / 1000)` |
| `attend10MinPercent` | `total10MinPlus / totalAttended` |
| `total30MinPlusPer1kInv` | `total30MinPlus / (invited / 1000)` |
| `attend30MinPercent` | `total30MinPlus / totalAttended` |

### Sales

| Field | Formula |
|-------|---------|
| `bookingsPerAttended` | `totalBookings / totalAttended` |
| `bookingsPerPast10Min` | `totalBookings / total10MinPlus` |
| `totalBookingsPer1kInv` | `totalBookings / (invited / 1000)` |
| `showPercent` | `shows / totalBookings` |
| `closeRatePercent` | `won / shows` (zero-safe) |
| `qualPercent` | `qualified / shows` (zero-safe) |

### Segment Name (display only)

```
segmentName = format(createdDate, "yyyy mmm dd") + ", " + industry + ", " + employeeRange + " employees, " + country
```

Returns `null` if any input field is missing.

## Null Display Rules

| Condition | API value | UI display |
|-----------|-----------|------------|
| Blank/empty source cell | `null` | `—` |
| Zero denominator in formula | `null` | `—` |
| Explicit numeric zero | `0` | `0` |
| Workbook `#DIV/0!` | `null` | `—` |

## Non-joiners

The Nonjoiners row is a **synthetic cohort**, not a Planning assignment. Its definition
lives in one place — `services/nonjoiners.py` — and is shared by the Statistics page
(`services/ghl_statistics_source.py`), the per-webinar report and weekly email
(`services/webinar_report.py`), the Planning viewer and the invite-list export, so all of
them report the same numbers.

> **One pool, everywhere.** There is deliberately no separate "send list" and "reporting
> cohort": the set the Planning page shows and the export CSV hands to the assistants is
> exactly the set the Statistics Nonjoiners row and the report measure. We report on who
> we actually invited and nobody else.
>
> The trade-off is accepted and expected: because converted contacts (booked / won /
> disqualified) are excluded from the pool, the Nonjoiners row's **sales columns read
> near-zero by construction** — contacts holding any GHL opportunity fell 53 → 5 in the
> W152 pool once suppression landed. That is correct, not a bug. Judge non-joiner sends on
> registrations and attendance. Do not "fix" this by re-adding bookers to the reporting
> cohort without changing the send list too — that would break the one-pool rule.

The pool for webinar **W** is built in five steps:

1. **Window** — the last **6** webinars before W (`NONJOINER_WINDOW`). Resolved by walking
   the `webinars.nonjoiner_source_webinar_id` chain back six hops, falling back to the
   next-lower `number` wherever a link is missing or points forward. A/B variants sharing
   a number collapse into one slot and contribute all of their broadcasts; a slot whose
   webinar has no `broadcast_id` contributes nothing but still consumes one of the six.
   **Webinars that haven't aired are stepped over entirely** (neither counted nor
   slot-consuming): aired = `date < CURRENT_DATE OR broadcast_auto_synced_at IS NOT NULL`.
   Without this, a broadcast synced ahead of time — `wg_sync` can be run manually — would
   dump all of its registrants into the next pool as fake no-shows and reset their
   invite counters.
2. **Stack** — every `webinargeek_subscribers` row across those broadcasts, deduped on
   `LOWER(email)` and collapsed per webinar number (joining either A/B variant of a
   number counts as joining that webinar).
3. **Keep the latest registration** — **every registration restarts the counter**, so only
   a contact's most recent registered number in the window decides: they are a non-joiner
   iff they did **not** join that one. Joined = `watched_live` OR `watched_replay` OR
   `minutes_viewing > 0`. Note this is wider than `ATT_PREDICATE` (which ignores replays):
   a replay-only viewer counts as joined for pool exclusion but not for attendance metrics.
   A contact who no-shows W150, joins W151, then registers for W152 and no-shows is a
   non-joiner again for W153 with a fresh six-invite budget.
4. **Remove suppressed** — one-way exits that re-registering does **not** undo (unlike
   attendance, which only quiets someone until they register again):
   - The **`blocklist` table**, matched by email alone. It is the canonical
     permanent-suppression list and already aggregates GHL unsubscribes (`ghl_dnd`),
     WebinarGeek unsubscribes (`wg_unsub`), manual UI additions and CSV imports. Matching
     it directly matters because roughly 9% of the pool has no `contacts` row at all —
     they came from WebinarGeek, not from one of our uploads — so a
     `contacts.is_blocklisted`-only check leaks.
   - `contacts.is_blocklisted`, and any `unsubscribed_at` on a window registration.
   - **Converted contacts**: booked a call (`ghl_contact.is_booked_call` or a
     `webinar_booking_attribution` row), deal **won**, or **disqualified**
     (`ghl_opportunity.pipeline_stage_id`). They've left the cold funnel.
5. **Remove already-planned** — contacts on a planned list for W. Cohort precedence is
   **planned > non-joiner > no-list-data**, so every contact lands in exactly one row.
   The Statistics page widens this to sibling A/B variants via its `tmp_nld_planned` temp
   table; the report scopes it to the single webinar.

The six-webinar window is the mechanical form of a **max-six-invites cap**: a contact who
registers for W146 and no-shows is invited as a non-joiner to W147–W152 — six invites —
and has aged out of the window by W153. Because the window is anchored on the latest
registration, re-registering slides it forward automatically; no per-contact counter is
stored anywhere.

Row metrics: `invited` = pool size (`actuallyUsed` is null, so rates fall back to it),
`totalRegs` / `totalAttended` = the cohort matched against **this** webinar's broadcast,
`yesMarked` / `maybeMarked` = responses from the uploaded Non-joiners CSV
(`webinar_nonjoiner_invites`), which only labels the derived cohort and never defines it.

> Before 2026-08-14 the Statistics page derived non-joiners from the previous webinar
> only, which undercut the pool roughly four-fold (W152: 2,041 pool / 790 regs, vs
> 7,502 / 966 under the shared definition). Statistics snapshots taken before that date
> still carry the old split until recomputed. The historical operational figure for W152
> was 974 regs; the eight-registrant difference is contacts who were mailed but should
> have been suppressed — blocklisted, or already booked/won/disqualified.

## Parent Aggregation Rules

Parent summary rows are **recomputed from child rows**, not copied from workbook parent-row formulas (which are broken in later webinars).

1. **Sum**: Most raw numeric metrics are summed across all children (including Nonjoiners and NO LIST DATA rows)
2. **Average**: `avgProjectedDealSize` — average of non-null child values
3. **Sum**: `avgClosedDealValue` — sum of non-null child values
4. **Sum**: `accountsNeeded` — sum of source-fed child values (not recomputed)
5. **Derive**: After aggregating raw counts, derived percentages/ratios are computed from the aggregated totals

## Workbook Anomalies

The following parent rows contain broken range references that spill into unrelated sections. This is why parent aggregation is semantic (sum children) rather than formula-copying:

- **W114, W115, W117**: Parent SUM formulas reference ranges beyond their child rows
- **W122**: `J153 = SUM(J154:J348)` — spills into webinars 123–136, producing `gcalInvited = 5,401,734` (correct value from rows 154–156 is `147,528`)
- **W136**: Parent row has stale/zeroed formulas; child rows 302–305 are valid but rows 306+ are scratch notes

## Future GoHighLevel Replacement Boundary

When GHL integration is added:

- **Changes**: `WorkbookMockStatisticsSource` is replaced with `GoHighLevelStatisticsSource` that fetches raw/source fields from the GHL API
- **Unchanged**: All derived metric formulas, parent aggregation logic, API response contract, frontend rendering, and this documentation
- The source adapter protocol in `services/statistics.py` defines the swap boundary
