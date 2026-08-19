"""074_tune_autovacuum_hot_tables

Per-table autovacuum tuning for the two tables the assign/claim path lives on.

Postgres' default `autovacuum_vacuum_scale_factor` is 0.2 — a table is vacuumed
once 20% of its rows are dead. That fraction is of the CURRENT row count, so the
threshold grows with the table and the problem gets worse exactly as the data
grows. Measured 2026-08-19:

    contacts                     5,600,348 live   →  ~1,120,000 dead before vacuum
    webinar_contact_memberships  4,043,956 live   →    ~808,000 dead before vacuum
                                 (311,026 dead already, last autovacuum 7 days prior)

The cost is not bloat, it is the VISIBILITY MAP. An Index Only Scan still has to
visit the heap for any page the VM does not mark all-visible, and the VM is only
refreshed by vacuum. With vacuum this far behind, index-only scans on `contacts`
were paying ~13,600 heap fetches apiece — on an instance that sustains only
~250-350 random reads/s, that alone is ~40-55s of pure IO per scan, and it is
what turns an index-only plan back into the random-read storm that migration 072
exists to avoid.

Lowering the scale factor to 0.02 makes vacuum run when ~2% of rows are dead
(~112k on contacts, ~81k on memberships) instead of 20%. Each run is far cheaper
because it has less to reclaim, the VM stays fresh so index-only scans stay
index-only, and — the point of the change — the trigger no longer degrades as
the tables grow.

analyze_scale_factor is lowered alongside it because the claim path's planner
estimates depend on distributions that shift with every assign
(assigned_membership_count, last_invited_at, outreach_status). Stale statistics
on those columns are how a covering index gets passed over for a heap scan.

autovacuum_vacuum_insert_scale_factor (PG13+) covers webinar_contact_memberships
specifically: it is append-mostly, so it accumulates few DEAD tuples but many
newly-inserted pages that the VM has never marked all-visible. Without an
insert-triggered vacuum those pages permanently defeat index-only scans against
it, no matter how healthy the dead-tuple ratio looks.

These are storage parameters — the ALTERs are catalog-only, take a brief
SHARE UPDATE EXCLUSIVE lock, rewrite nothing, and are safe to apply online.

Deliberately NOT included: ghl_contact and webinar_calendar_invites (both 3M+
rows and also behind index-only scans, see migration 069). They belong to the
statistics path rather than the claim path and should be measured on their own
before being tuned.

Revision ID: 074
Revises: 073
"""
from alembic import op

revision = "074"
down_revision = "073"
branch_labels = None
depends_on = None

TUNED = {
    "contacts": {
        "autovacuum_vacuum_scale_factor": "0.02",
        "autovacuum_analyze_scale_factor": "0.01",
        # Prod already carried this one, set by hand and never captured in a
        # migration — folded in here so a fresh environment reproduces it.
        # It earns its place independently: contacts takes large CSV imports,
        # so it is append-heavy in bursts, and inserted pages the VM has never
        # marked all-visible defeat index-only scans just as thoroughly as dead
        # tuples do.
        "autovacuum_vacuum_insert_scale_factor": "0.02",
    },
    "webinar_contact_memberships": {
        "autovacuum_vacuum_scale_factor": "0.02",
        "autovacuum_analyze_scale_factor": "0.01",
        "autovacuum_vacuum_insert_scale_factor": "0.05",
    },
}


def upgrade() -> None:
    # Lock-tolerant for the same reason as migration 073: this runs from the
    # Render start command, so it must never be why the service fails to boot.
    # ALTER TABLE ... SET takes SHARE UPDATE EXCLUSIVE, which does NOT conflict
    # with ordinary reads or writes — but it does conflict with an in-progress
    # autovacuum on the same table, which on a table this size can run for
    # minutes. Skipping is safe: the tables keep their previous (default)
    # thresholds until the next attempt, which is exactly the status quo.
    for table, params in TUNED.items():
        settings = ", ".join(f"{k} = {v}" for k, v in params.items())
        op.execute(
            f"""
            DO $$
            BEGIN
                SET LOCAL lock_timeout = '5s';
                EXECUTE 'ALTER TABLE {table} SET ({settings})';
            EXCEPTION
                WHEN lock_not_available THEN
                    RAISE NOTICE 'skipped autovacuum tuning for {table}: lock busy';
            END $$;
            """
        )


def downgrade() -> None:
    for table, params in TUNED.items():
        names = ", ".join(params)
        op.execute(f"ALTER TABLE {table} RESET ({names})")
