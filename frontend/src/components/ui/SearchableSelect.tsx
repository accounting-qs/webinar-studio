"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
  /** When true the option renders greyed out and cannot be chosen. */
  disabled?: boolean;
}

/**
 * Single-select dropdown with type-to-filter search. Modeled on the Planning
 * page's inline Dropdown but kept standalone so the Upload mapping step can
 * reuse it without touching Planning. Disabled options stay visible (greyed)
 * so the user can see why a field is unavailable — used here to prevent the
 * same DB field being mapped to two CSV columns.
 */
export function SearchableSelect({
  options,
  value,
  onChange,
  placeholder = "Select...",
  className = "",
}: {
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) { setOpen(false); setQuery(""); }
    }
    if (open) document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  // Focus the search box as soon as the menu opens.
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const selected = options.find((o) => o.value === value);
  const q = query.trim().toLowerCase();
  const filtered = q ? options.filter((o) => o.label.toLowerCase().includes(q)) : options;

  const choose = (opt: SelectOption) => {
    if (opt.disabled) return;
    onChange(opt.value);
    setOpen(false);
    setQuery("");
  };

  return (
    <div ref={ref} className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-sm text-left flex items-center justify-between gap-2 focus:outline-none focus:ring-1 focus:ring-violet-500 transition-colors"
      >
        <span className={cn("truncate", selected ? "text-zinc-800 dark:text-zinc-200" : "text-zinc-500")}>
          {selected ? selected.label : placeholder}
        </span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          className={cn("text-zinc-500 shrink-0 transition-transform duration-150", open && "rotate-180")}>
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-full mt-1 z-50 bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg shadow-xl shadow-black/10 dark:shadow-black/40 flex flex-col max-h-[280px]">
          <div className="p-1.5 border-b border-zinc-200 dark:border-zinc-800/60 shrink-0">
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search…"
              className="w-full bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
          </div>
          <div className="overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-3 py-2 text-xs text-zinc-500">No matches</div>
            ) : (
              filtered.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  disabled={o.disabled}
                  onClick={() => choose(o)}
                  className={cn(
                    "w-full text-left px-3 py-1.5 text-sm transition-colors truncate",
                    o.disabled
                      ? "text-zinc-400 dark:text-zinc-600 opacity-50 cursor-not-allowed"
                      : o.value === value
                      ? "bg-violet-500/10 text-violet-600 dark:text-violet-400 font-medium"
                      : "text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/60",
                  )}
                >
                  {o.label}
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
