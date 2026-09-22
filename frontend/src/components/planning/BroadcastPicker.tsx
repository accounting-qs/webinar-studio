"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { WebinarPlatform, WgWebinar } from "@/lib/api";

/**
 * Searchable webinar/broadcast picker, shared by the New Webinar and Edit
 * Webinar modals.
 *
 * A native <select> cannot be typed into, and the lists are long enough that
 * scrolling for a specific webinar is painful — Zoom titles in particular are
 * full marketing sentences that truncate long before the date or id are
 * visible. So this is a combobox: a filter box over a grouped list.
 *
 * Label order differs per platform on purpose:
 *   WebinarGeek — title · date · id   (unchanged; it reads well already)
 *   Zoom        — date · id · title   (the two short, identifying fields first,
 *                                      so a 120-character title cannot push
 *                                      them out of view)
 * Either way the search matches across all of them.
 */

export type BroadcastPickerOption = {
  id: string;
  /** What the row renders. */
  label: string;
  /** Lowercased haystack: title, date, id, raw id. */
  search: string;
  startsAt: string | null;
};

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

/** Zoom ids are namespaced `zoom:<id>[:<occurrence>]`; show the Zoom-facing part. */
function zoomId(broadcastId: string): string {
  return broadcastId.replace(/^zoom:/, "");
}

export function toOptions(
  broadcasts: WgWebinar[],
  platform: WebinarPlatform,
): BroadcastPickerOption[] {
  return broadcasts.map((b) => {
    const title = b.internal_title || b.name || "";
    const date = fmtDate(b.starts_at);
    const label =
      platform === "zoom"
        ? [date, zoomId(b.broadcast_id), title].filter(Boolean).join(" · ")
        : [title || `Broadcast ${b.broadcast_id}`, date, b.broadcast_id]
            .filter(Boolean)
            .join(" · ");
    return {
      id: b.broadcast_id,
      label,
      // Raw id included as well, so pasting "zoom:8381…" finds it too.
      search: [title, date, b.broadcast_id, zoomId(b.broadcast_id), b.starts_at ?? ""]
        .join(" ")
        .toLowerCase(),
      startsAt: b.starts_at,
    };
  });
}

export function BroadcastPicker({
  value,
  options,
  loading,
  onChange,
  noneLabel = "— None —",
  placeholder = "Search by name, date or ID…",
}: {
  value: string;
  options: BroadcastPickerOption[];
  loading?: boolean;
  onChange: (id: string) => void;
  noneLabel?: string;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  // The past/upcoming boundary. Read in an effect rather than during render —
  // Date.now() is impure, and re-reading it on open keeps a modal left sitting
  // open from mislabelling a webinar that has since started.
  const [nowTs, setNowTs] = useState(() => Date.now());
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // A linked broadcast that is not in the current list (different account, or
  // deleted upstream) must stay selectable rather than silently resetting.
  const selected = options.find((o) => o.id === value);
  const orphan = value && !selected ? value : "";

  const { upcoming, past } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hits = q ? options.filter((o) => o.search.includes(q)) : options;
    const now = nowTs;
    const up: BroadcastPickerOption[] = [];
    const pa: BroadcastPickerOption[] = [];
    for (const o of hits) {
      const t = o.startsAt ? new Date(o.startsAt).getTime() : NaN;
      // Undated webinars sit with upcoming: they have not demonstrably happened.
      if (isNaN(t) || t >= now) up.push(o);
      else pa.push(o);
    }
    up.sort((a, b) => (a.startsAt || "").localeCompare(b.startsAt || ""));
    pa.sort((a, b) => (b.startsAt || "").localeCompare(a.startsAt || ""));
    return { upcoming: up, past: pa };
  }, [options, query, nowTs]);

  // Flat order drives keyboard nav; index 0 is always the "none" row.
  const flat = useMemo(
    () => [{ id: "", label: noneLabel, search: "", startsAt: null }, ...upcoming, ...past],
    [upcoming, past, noneLabel],
  );

  /** Every close goes through here so the query never survives into the next
   *  open — resetting it in an effect instead would cascade a render. */
  function close() {
    setOpen(false);
    setQuery("");
    setCursor(0);
  }

  function pick(id: string) {
    onChange(id);
    close();
  }

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const buttonText = selected ? selected.label : orphan ? `Current · ${orphan}` : noneLabel;

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        disabled={loading}
        onClick={() => {
          if (open) {
            close();
            return;
          }
          // Re-read the past/upcoming boundary here rather than during render.
          setNowTs(Date.now());
          setOpen(true);
        }}
        className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-left text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors disabled:opacity-60 flex items-center gap-2"
      >
        <span className={"flex-1 truncate " + (selected || orphan ? "" : "text-zinc-500")}>
          {loading ? "loading…" : buttonText}
        </span>
        <span className="text-zinc-400 text-xs">▾</span>
      </button>

      {open && (
        <div className="absolute z-50 mt-1 w-full rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-xl">
          <div className="p-2 border-b border-zinc-200 dark:border-zinc-800">
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                // Reset the highlight with the query itself rather than in an
                // effect, which would be a cascading render.
                setCursor(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  close();
                } else if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setCursor((c) => Math.min(c + 1, flat.length - 1));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setCursor((c) => Math.max(c - 1, 0));
                } else if (e.key === "Enter") {
                  e.preventDefault();
                  const o = flat[cursor];
                  if (o) pick(o.id);
                }
              }}
              placeholder={placeholder}
              className="w-full px-2.5 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-900 dark:text-zinc-100"
            />
          </div>

          <div className="max-h-72 overflow-y-auto py-1">
            <Row
              option={{ id: "", label: noneLabel, search: "", startsAt: null }}
              active={cursor === 0}
              selected={!value}
              onPick={pick}
              onHover={() => setCursor(0)}
            />

            {upcoming.length > 0 && <GroupLabel>Upcoming</GroupLabel>}
            {upcoming.map((o, i) => (
              <Row
                key={o.id}
                option={o}
                active={cursor === i + 1}
                selected={o.id === value}
                onPick={pick}
                onHover={() => setCursor(i + 1)}
              />
            ))}

            {past.length > 0 && <GroupLabel>Past</GroupLabel>}
            {past.map((o, i) => (
              <Row
                key={o.id}
                option={o}
                active={cursor === upcoming.length + 1 + i}
                selected={o.id === value}
                onPick={pick}
                onHover={() => setCursor(upcoming.length + 1 + i)}
              />
            ))}

            {upcoming.length === 0 && past.length === 0 && (
              <div className="px-3 py-3 text-xs text-zinc-500">
                {options.length === 0
                  ? "No webinars cached — refresh the list on the Connectors page."
                  : `Nothing matches “${query}”.`}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-zinc-400 dark:text-zinc-500 font-medium">
      {children}
    </div>
  );
}

function Row({
  option,
  active,
  selected,
  onPick,
  onHover,
}: {
  option: BroadcastPickerOption;
  active: boolean;
  selected: boolean;
  onPick: (id: string) => void;
  onHover: () => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(option.id)}
      onMouseEnter={onHover}
      className={
        "w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 " +
        (active ? "bg-violet-500/10 " : "") +
        (selected ? "text-violet-600 dark:text-violet-400 font-medium" : "text-zinc-700 dark:text-zinc-300")
      }
    >
      <span className="w-3 shrink-0">{selected ? "✓" : ""}</span>
      <span className="truncate" title={option.label}>{option.label}</span>
    </button>
  );
}
