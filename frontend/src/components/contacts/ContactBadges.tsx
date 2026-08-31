/** Small shared badges for the Contacts directory table + detail drawer. */

export function StatusBadge({ status }: { status: "available" | "assigned" | "used" | null }) {
  if (!status) return <span className="text-zinc-400 text-xs">—</span>;
  const styles =
    status === "used"
      ? "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20"
      : status === "assigned"
        ? "bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-200 dark:border-violet-500/20"
        : "bg-zinc-100 dark:bg-zinc-800 text-zinc-500 dark:text-zinc-400 border-zinc-200 dark:border-zinc-700";
  return (
    <span className={`inline-flex items-center text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full border ${styles}`}>
      {status}
    </span>
  );
}

export function BlocklistedBadge() {
  return (
    <span className="inline-flex items-center text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full border bg-red-100 dark:bg-red-500/15 text-red-600 dark:text-red-400 border-red-200 dark:border-red-500/20">
      Blocklisted
    </span>
  );
}

/** Calendar invite response — free text from the CSV, color-coded by intent. */
export function ResponseBadge({ response }: { response: string | null }) {
  if (!response) return <span className="text-zinc-400 text-xs">—</span>;
  const v = response.trim().toLowerCase();
  const styles = v.startsWith("yes") || v.startsWith("accept")
    ? "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20"
    : v.startsWith("no") || v.startsWith("decline")
      ? "bg-rose-100 dark:bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-200 dark:border-rose-500/20"
      : v.startsWith("maybe") || v.startsWith("tentative")
        ? "bg-amber-100 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-200 dark:border-amber-500/20"
        : "bg-zinc-100 dark:bg-zinc-800 text-zinc-500 dark:text-zinc-400 border-zinc-200 dark:border-zinc-700";
  return (
    <span className={`inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-full border ${styles}`}>
      {response}
    </span>
  );
}

/** "Jan 15, 2026" from an ISO date or datetime string. */
export function fmtDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  if (isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export function webinarLabel(number: number | null, variant: string | null): string {
  if (number == null) return "—";
  return variant ? `W${number} · ${variant}` : `W${number}`;
}
