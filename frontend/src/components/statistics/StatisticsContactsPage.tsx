"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  fetchStatisticsContacts,
  type ContactDrilldownResponse,
} from "@/lib/api";
import { METRIC_COLUMNS } from "./metricRegistry";
import { ContactsDrilldownTable } from "./ContactsDrilldownTable";

function metricLabel(key: string): string {
  const col = METRIC_COLUMNS.find((c) => c.key === key);
  if (!col) return key;
  return `${col.group} · ${col.label}`;
}

export function StatisticsContactsPage() {
  const params = useSearchParams();
  // Prefer webinar_id (UUID) — disambiguates A/B variants. Fall back to
  // the legacy `webinar` (number) param for any old links still around.
  const webinarId = params.get("webinar_id");
  const webinarNumber = params.get("webinar") ? Number(params.get("webinar")) : null;
  const metric = params.get("metric") ?? "";
  const assignment = params.get("assignment") ?? null;
  const listLabel = params.get("list") ?? null; // optional display label

  const [data, setData] = useState<ContactDrilldownResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if ((!webinarId && !webinarNumber) || !metric) {
      setLoading(false);
      setError("Missing required query params: webinar_id (or webinar) and metric");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetchStatisticsContacts({ webinarId, webinarNumber, metric, assignment });
        if (cancelled) return;
        setData(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [webinarId, webinarNumber, metric, assignment]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="flex items-center gap-3">
          <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-zinc-500">Loading contacts...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return <div className="px-6 py-12 text-sm text-red-400">{error}</div>;
  }

  if (!data) return null;

  return (
    <div>
      <div className="sticky top-12 z-40 bg-white dark:bg-zinc-950/90 backdrop-blur-md border-b border-zinc-200 dark:border-zinc-800/40 px-6 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">
            Webinar {data.webinar_number} · {metricLabel(data.metric)}
          </h1>
          <span className="text-xs text-zinc-500">
            {listLabel ? `list: ${listLabel} · ` : ""}{data.total} {data.unit === "opportunity" ? "opportunities" : "contacts"}
          </span>
        </div>
      </div>

      <div className="px-6 py-6 max-w-5xl">
        {!data.available && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-500 mb-4">
            {data.reason ?? "Drill-down unavailable for this metric."}
          </div>
        )}

        <ContactsDrilldownTable data={data} />
      </div>
    </div>
  );
}
