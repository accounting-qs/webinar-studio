"use client";

/**
 * Chart primitives for the Statistics dashboards — plain SVG, no chart library.
 *
 * Colour is not chosen by taste here. Three palettes, each picked for the job
 * its chart does and each run through the data-viz validator (OKLab ΔE for
 * colour-vision deficiency, normal-vision separation, contrast vs the surface
 * the chart actually renders on — white in light mode, ~#121214 in dark):
 *
 *   categorical  — identity (one hue per line series). Fixed slot order, never
 *                  cycled; capped at 4 series, then the rest folds into "Other".
 *   funnel       — ordinal (stage order). One blue hue, monotone lightness,
 *                  every adjacent gap ≥ 0.06 L so the order is legible without
 *                  reading the labels.
 *   quality      — diverging (Great … Bad/DQ). Green and red poles are the dark
 *                  ends, the two middle tiers are the light ones, so the arms
 *                  read as opposite and CVD readers still get the order from
 *                  lightness. Every tier is direct-labelled — colour never
 *                  carries the meaning alone.
 *
 * Both modes are selected, not flipped: the dark steps were re-picked against
 * the dark surface and validated as their own set.
 */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/* ── palette ───────────────────────────────────────────────────────────── */

export const VIZ_STYLES = `
.viz {
  --v-grid: #e4e4e7;
  --v-axis: #d4d4d8;
  --v-ink: #3f3f46;
  --v-muted: #71717a;
  --v-surface: #ffffff;
  --v-cat-1: #2a78d6;
  --v-cat-2: #eb6834;
  --v-cat-3: #1baf7a;
  --v-cat-4: #eda100;
  --v-fun-1: #82b9ff;
  --v-fun-2: #6da2f2;
  --v-fun-3: #588cda;
  --v-fun-4: #4477c3;
  --v-fun-5: #2f62ac;
  --v-fun-6: #1b4d95;
  --v-q-great: #0e5f30;
  --v-q-ok: #5cc78a;
  --v-q-barely: #e08e3c;
  --v-q-bad: #a01f1f;
  --v-q-unrated: #a1a1aa;
}
.dark .viz {
  --v-grid: #27272a;
  --v-axis: #3f3f46;
  --v-ink: #d4d4d8;
  --v-muted: #a1a1aa;
  --v-surface: #121214;
  --v-cat-1: #3987e5;
  --v-cat-2: #d95926;
  --v-cat-3: #199e70;
  --v-cat-4: #c98500;
  --v-fun-1: #c0d8fb;
  --v-fun-2: #a1c3f5;
  --v-fun-3: #85aeeb;
  --v-fun-4: #6b99df;
  --v-fun-5: #5385cf;
  --v-fun-6: #3e71bc;
  --v-q-great: #2f9e5c;
  --v-q-ok: #8fe0ae;
  --v-q-barely: #edb05a;
  --v-q-bad: #d8483c;
  --v-q-unrated: #71717a;
}
`;

/** Renders the palette once per page. Mount inside the page's root element. */
export function VizStyles() {
  return <style dangerouslySetInnerHTML={{ __html: VIZ_STYLES }} />;
}

/** Categorical slots, in fixed order — assign by series identity, never cycle. */
export const CAT = ["var(--v-cat-1)", "var(--v-cat-2)", "var(--v-cat-3)", "var(--v-cat-4)"];
/** Ordinal funnel ramp, light → dark = first stage → last stage. */
export const FUNNEL_RAMP = [
  "var(--v-fun-1)", "var(--v-fun-2)", "var(--v-fun-3)",
  "var(--v-fun-4)", "var(--v-fun-5)", "var(--v-fun-6)",
];
/** Diverging lead-quality tiers, best → worst, plus the neutral unrated slot. */
export const QUALITY_TIERS = [
  { key: "great", label: "Great", color: "var(--v-q-great)" },
  { key: "ok", label: "Ok", color: "var(--v-q-ok)" },
  { key: "barely", label: "Barely", color: "var(--v-q-barely)" },
  { key: "bad", label: "Bad / DQ", color: "var(--v-q-bad)" },
  { key: "unrated", label: "Unrated", color: "var(--v-q-unrated)" },
] as const;

/* ── formatting ────────────────────────────────────────────────────────── */

export type ValueFormat = "int" | "pct" | "ratio" | "per1k";

export function fmtValue(v: number | null | undefined, f: ValueFormat): string {
  if (v == null || !Number.isFinite(v)) return "—";
  switch (f) {
    case "pct": return `${(v * 100).toFixed(1)}%`;
    case "ratio": return v.toFixed(2);
    // Booking yields sit well under 1 per 1,000 — one decimal would round every
    // segment to the same "0.1" and erase the ranking the chart exists to show.
    case "per1k": return v.toFixed(Math.abs(v) < 1 ? 2 : 1);
    default: return Math.round(v).toLocaleString();
  }
}

/** Decimals needed for `step` to be visible in the tick text — otherwise an
 * axis whose whole range is under 1 prints the same rounded label repeatedly. */
function tickDecimals(step: number): number {
  if (!(step > 0)) return 0;
  return Math.max(0, Math.min(4, Math.ceil(-Math.log10(step))));
}

/** Compact axis ticks — 12.4k rather than 12,400. */
function fmtTick(v: number, f: ValueFormat, step = 0): string {
  if (f === "pct") {
    const d = tickDecimals(step * 100);
    return `${(v * 100).toFixed(d)}%`;
  }
  if (f === "ratio" || f === "per1k") return v.toFixed(tickDecimals(step));
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(v >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(v));
}

/** "Nice" upper bound + evenly spaced ticks, so the axis reads in round steps. */
function niceScale(max: number, count = 4): { max: number; ticks: number[]; step: number } {
  if (!(max > 0)) return { max: 1, ticks: [0, 1], step: 1 };
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const top = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= top + step / 2; t += step) ticks.push(Number(t.toFixed(10)));
  return { max: top, ticks, step };
}

/* ── layout helpers ────────────────────────────────────────────────────── */

/** Element width, tracked so the SVG can be laid out in real pixels (no
 * preserveAspectRatio stretching, which would distort every label). */
function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(720);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(el);
    setW(el.getBoundingClientRect().width);
    return () => ro.disconnect();
  }, []);
  return [ref, Math.max(w, 280)];
}

/* ── chrome ────────────────────────────────────────────────────────────── */

export function ChartCard({
  title, subtitle, right, children, className,
}: {
  title: string;
  subtitle?: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`viz rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 p-4 ${className ?? ""}`}
    >
      <header className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h3 className="text-[13px] font-semibold text-zinc-900 dark:text-zinc-100">{title}</h3>
          {subtitle && <p className="text-[11px] text-zinc-500 mt-0.5">{subtitle}</p>}
        </div>
        {right && <div className="shrink-0">{right}</div>}
      </header>
      {children}
    </section>
  );
}

export function StatTile({
  label, value, sub, accent,
}: {
  label: string;
  value: string;
  sub?: string;
  /** Optional swatch tying the tile to a chart series. */
  accent?: string;
}) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 px-3 py-2">
      <div className="flex items-center gap-1.5">
        {accent && (
          <span className="w-2 h-2 rounded-[2px] shrink-0" style={{ background: accent }} aria-hidden />
        )}
        <div className="text-[9px] uppercase tracking-wider text-zinc-500 font-semibold truncate">{label}</div>
      </div>
      <div className="text-xl font-bold text-zinc-900 dark:text-zinc-100 mt-0.5">{value}</div>
      {sub && <div className="text-[10px] text-zinc-500 truncate">{sub}</div>}
    </div>
  );
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <ul className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-[2px]" style={{ background: it.color }} aria-hidden />
          <span className="text-[11px] text-zinc-600 dark:text-zinc-400">{it.label}</span>
        </li>
      ))}
    </ul>
  );
}

export function EmptyChart({ message }: { message: string }) {
  return (
    <div className="h-32 flex items-center justify-center text-[12px] text-zinc-500">{message}</div>
  );
}

/* ── tooltip ───────────────────────────────────────────────────────────── */

type Tip = { x: number; y: number; title: string; rows: { label: string; value: string; color?: string }[] };

function Tooltip({ tip, width }: { tip: Tip | null; width: number }) {
  if (!tip) return null;
  // Flip to the left of the cursor near the right edge so the card never clips.
  const flip = tip.x > width - 160;
  return (
    <div
      className="pointer-events-none absolute z-20 rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-lg px-2.5 py-1.5 min-w-[120px]"
      style={{
        left: flip ? undefined : tip.x + 12,
        right: flip ? width - tip.x + 12 : undefined,
        top: Math.max(0, tip.y - 12),
      }}
    >
      <div className="text-[10px] font-semibold text-zinc-900 dark:text-zinc-100 mb-1">{tip.title}</div>
      {tip.rows.map((r) => (
        <div key={r.label} className="flex items-center gap-1.5 text-[11px] leading-4">
          {r.color && <span className="w-2 h-2 rounded-[2px] shrink-0" style={{ background: r.color }} aria-hidden />}
          <span className="text-zinc-500 flex-1 whitespace-nowrap">{r.label}</span>
          <span className="text-zinc-900 dark:text-zinc-100 font-semibold tabular-nums">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

/* ── funnel ────────────────────────────────────────────────────────────── */

export interface FunnelStage {
  label: string;
  value: number | null;
  /** Optional override for the step-down caption (defaults to % of prior stage). */
  note?: string;
}

/**
 * Stage-by-stage drop-off. Bar length is the **step-down from the stage above**
 * — the share of the previous row that survived — not the share of the first
 * stage.
 *
 * That choice is forced by the data: cold outreach registers well under 1% of
 * everyone invited, so scaling to the first stage collapses every stage after
 * "Registered" to a hairline and the chart stops saying anything. Scaling to
 * the previous stage keeps each row readable and makes the biggest drop
 * obvious at a glance. The bar and the percentage beside it therefore encode
 * the same number, and the absolute count keeps its own column.
 */
export function FunnelChart({ stages }: { stages: FunnelStage[] }) {
  const first = stages.find((s) => s.value != null)?.value ?? 0;
  if (!first) return <EmptyChart message="No funnel data for this selection." />;
  return (
    <div className="viz space-y-1.5">
      {stages.map((s, i) => {
        const v = s.value ?? 0;
        const prev = i === 0 ? null : stages[i - 1].value ?? null;
        const stepDown = prev != null && prev > 0 ? v / prev : null;
        // First row is the baseline everything else is measured against.
        const share = i === 0 ? 1 : stepDown ?? 0;
        const pct = Math.max(0, Math.min(100, share * 100));
        const color = FUNNEL_RAMP[Math.min(i, FUNNEL_RAMP.length - 1)];
        return (
          <div key={s.label} className="grid grid-cols-[124px_1fr_84px_64px] items-center gap-3">
            <div className="text-[11px] text-zinc-600 dark:text-zinc-400 truncate" title={s.label}>
              {s.label}
            </div>
            <div className="h-6 rounded bg-zinc-100 dark:bg-zinc-800/60 overflow-hidden">
              <div className="h-full rounded-r-[4px]" style={{ width: `${pct}%`, background: color }} />
            </div>
            {/* The count sits beside the bar, not on it: the ramp spans light to
                dark, so no single ink colour stays legible on every stage. */}
            <div className="text-[11px] text-right tabular-nums font-semibold text-zinc-900 dark:text-zinc-100">
              {fmtValue(s.value, "int")}
            </div>
            <div className="text-[11px] text-right tabular-nums text-zinc-500">
              {s.note ?? (stepDown != null ? `${(stepDown * 100).toFixed(1)}%` : "—")}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── line chart ────────────────────────────────────────────────────────── */

export interface LineSeries {
  key: string;
  label: string;
  values: (number | null)[];
  color: string;
}

/**
 * Change over time. One y-scale only — series that don't share a unit belong in
 * a separate chart, never on a second axis. The last point of each series is
 * direct-labelled so identity survives without hunting through the legend.
 */
export function LineChart({
  xLabels, series, format = "pct", height = 220,
}: {
  xLabels: string[];
  series: LineSeries[];
  format?: ValueFormat;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const all = series.flatMap((s) => s.values).filter((v): v is number => v != null);
  if (!all.length || xLabels.length === 0) {
    return <EmptyChart message="No data for this selection." />;
  }

  const padL = 42, padR = 58, padT = 10, padB = 26;
  const w = width, h = height;
  const iw = Math.max(10, w - padL - padR);
  const ih = h - padT - padB;
  const { max, ticks, step } = niceScale(Math.max(...all), 4);
  const n = xLabels.length;
  const xAt = (i: number) => padL + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const yAt = (v: number) => padT + ih - (v / max) * ih;

  // Thin the x tick labels so they never collide.
  const every = Math.max(1, Math.ceil(n / Math.floor(iw / 54)));

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const i = n === 1 ? 0 : Math.round(((x - padL) / iw) * (n - 1));
    const idx = Math.max(0, Math.min(n - 1, i));
    setHoverIdx(idx);
    setTip({
      x, y: e.clientY - rect.top,
      title: xLabels[idx],
      rows: series.map((s) => ({ label: s.label, value: fmtValue(s.values[idx], format), color: s.color })),
    });
  };

  return (
    <div ref={ref} className="viz relative">
      <svg
        width={w} height={h} role="img"
        onMouseMove={onMove}
        onMouseLeave={() => { setTip(null); setHoverIdx(null); }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={padL + iw} y1={yAt(t)} y2={yAt(t)} stroke="var(--v-grid)" strokeWidth={1} />
            <text x={padL - 6} y={yAt(t) + 3} textAnchor="end" fontSize={10} fill="var(--v-muted)">
              {fmtTick(t, format, step)}
            </text>
          </g>
        ))}
        {xLabels.map((lab, i) =>
          i % every === 0 || i === n - 1 ? (
            <text key={`${lab}-${i}`} x={xAt(i)} y={h - 8} textAnchor="middle" fontSize={10} fill="var(--v-muted)">
              {lab}
            </text>
          ) : null,
        )}
        {hoverIdx != null && (
          <line
            x1={xAt(hoverIdx)} x2={xAt(hoverIdx)} y1={padT} y2={padT + ih}
            stroke="var(--v-axis)" strokeWidth={1} strokeDasharray="3 3"
          />
        )}
        {series.map((s) => {
          // Break the path on gaps so a missing webinar isn't drawn through.
          const segs: string[] = [];
          let cur: string[] = [];
          s.values.forEach((v, i) => {
            if (v == null) { if (cur.length) segs.push(cur.join(" ")); cur = []; return; }
            cur.push(`${cur.length ? "L" : "M"}${xAt(i)},${yAt(v)}`);
          });
          if (cur.length) segs.push(cur.join(" "));
          const lastIdx = s.values.reduce<number>((acc, v, i) => (v != null ? i : acc), -1);
          return (
            <g key={s.key}>
              {segs.map((d, i) => (
                <path key={i} d={d} fill="none" stroke={s.color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
              ))}
              {s.values.map((v, i) =>
                v == null ? null : (
                  <circle
                    key={i} cx={xAt(i)} cy={yAt(v)} r={hoverIdx === i ? 4.5 : 2.5}
                    fill={s.color} stroke="var(--v-surface)" strokeWidth={2}
                  />
                ),
              )}
              {lastIdx >= 0 && (
                <text
                  x={xAt(lastIdx) + 8} y={yAt(s.values[lastIdx]!) + 3}
                  fontSize={10} fontWeight={600} fill="var(--v-ink)"
                >
                  {s.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} width={w} />
    </div>
  );
}

/* ── vertical bars (single series) ─────────────────────────────────────── */

export function BarChart({
  bars, format = "int", height = 200, color = "var(--v-cat-1)", valueLabel,
}: {
  bars: { label: string; value: number | null; tipRows?: { label: string; value: string }[] }[];
  format?: ValueFormat;
  height?: number;
  color?: string;
  /** Row label used in the tooltip (the chart title names the measure). */
  valueLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip | null>(null);
  const vals = bars.map((b) => b.value).filter((v): v is number => v != null);
  if (!vals.length) return <EmptyChart message="No data for this selection." />;

  const padL = 42, padR = 8, padT = 10, padB = 26;
  const w = width, h = height;
  const iw = Math.max(10, w - padL - padR);
  const ih = h - padT - padB;
  const { max, ticks, step } = niceScale(Math.max(...vals), 4);
  const n = bars.length;
  const slot = iw / n;
  // 2px of surface between neighbours — the spacer that keeps bars distinct.
  const bw = Math.max(3, Math.min(34, slot - 2));
  const every = Math.max(1, Math.ceil(n / Math.floor(iw / 46)));

  return (
    <div ref={ref} className="viz relative">
      <svg width={w} height={h} role="img">
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={padL} x2={padL + iw}
              y1={padT + ih - (t / max) * ih} y2={padT + ih - (t / max) * ih}
              stroke="var(--v-grid)" strokeWidth={1}
            />
            <text
              x={padL - 6} y={padT + ih - (t / max) * ih + 3}
              textAnchor="end" fontSize={10} fill="var(--v-muted)"
            >
              {fmtTick(t, format, step)}
            </text>
          </g>
        ))}
        {bars.map((b, i) => {
          const cx = padL + slot * i + slot / 2;
          const bh = b.value == null ? 0 : Math.max(0, (b.value / max) * ih);
          return (
            <g key={`${b.label}-${i}`}>
              <rect
                x={cx - bw / 2} y={padT + ih - bh} width={bw} height={bh}
                rx={Math.min(4, bw / 2)} fill={color}
              />
              {/* Hit target wider than the mark. */}
              <rect
                x={cx - slot / 2} y={padT} width={slot} height={ih} fill="transparent"
                onMouseEnter={(e) => {
                  const rect = e.currentTarget.ownerSVGElement!.getBoundingClientRect();
                  setTip({
                    x: cx, y: padT + ih - bh - 8,
                    title: b.label,
                    rows: b.tipRows ?? [{ label: valueLabel, value: fmtValue(b.value, format), color }],
                  });
                  void rect;
                }}
                onMouseLeave={() => setTip(null)}
              />
              {(i % every === 0 || i === n - 1) && (
                <text x={cx} y={h - 8} textAnchor="middle" fontSize={10} fill="var(--v-muted)">
                  {b.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} width={w} />
    </div>
  );
}

/* ── stacked bars ──────────────────────────────────────────────────────── */

export function StackedBarChart({
  categories, segments, height = 220, format = "int",
}: {
  categories: { label: string; values: Record<string, number | null>; total?: number | null }[];
  segments: { key: string; label: string; color: string }[];
  height?: number;
  format?: ValueFormat;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip | null>(null);

  const totals = categories.map((c) =>
    segments.reduce((s, seg) => s + (c.values[seg.key] ?? 0), 0),
  );
  if (!totals.some((t) => t > 0)) return <EmptyChart message="No booked calls in this selection." />;

  const padL = 36, padR = 8, padT = 10, padB = 26;
  const w = width, h = height;
  const iw = Math.max(10, w - padL - padR);
  const ih = h - padT - padB;
  const { max, ticks, step } = niceScale(Math.max(...totals), 4);
  const n = categories.length;
  const slot = iw / n;
  const bw = Math.max(4, Math.min(38, slot - 2));
  const every = Math.max(1, Math.ceil(n / Math.floor(iw / 46)));

  return (
    <div ref={ref} className="viz relative">
      <svg width={w} height={h} role="img">
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={padL} x2={padL + iw}
              y1={padT + ih - (t / max) * ih} y2={padT + ih - (t / max) * ih}
              stroke="var(--v-grid)" strokeWidth={1}
            />
            <text
              x={padL - 6} y={padT + ih - (t / max) * ih + 3}
              textAnchor="end" fontSize={10} fill="var(--v-muted)"
            >
              {fmtTick(t, format, step)}
            </text>
          </g>
        ))}
        {categories.map((c, i) => {
          const cx = padL + slot * i + slot / 2;
          let acc = 0;
          return (
            <g key={`${c.label}-${i}`}>
              {segments.map((seg) => {
                const v = c.values[seg.key] ?? 0;
                if (v <= 0) return null;
                const y0 = padT + ih - ((acc + v) / max) * ih;
                const y1 = padT + ih - (acc / max) * ih;
                acc += v;
                // 2px surface gap between stacked segments.
                const segH = Math.max(1, y1 - y0 - 2);
                return (
                  <rect
                    key={seg.key} x={cx - bw / 2} y={y0} width={bw} height={segH}
                    rx={2} fill={seg.color}
                  />
                );
              })}
              <rect
                x={cx - slot / 2} y={padT} width={slot} height={ih} fill="transparent"
                onMouseEnter={() =>
                  setTip({
                    x: cx, y: padT + ih - (totals[i] / max) * ih - 8,
                    title: c.label,
                    rows: [
                      ...segments
                        .filter((seg) => (c.values[seg.key] ?? 0) > 0)
                        .map((seg) => ({
                          label: seg.label,
                          value: fmtValue(c.values[seg.key], format),
                          color: seg.color,
                        })),
                      { label: "Total", value: fmtValue(totals[i], format) },
                    ],
                  })
                }
                onMouseLeave={() => setTip(null)}
              />
              {(i % every === 0 || i === n - 1) && (
                <text x={cx} y={h - 8} textAnchor="middle" fontSize={10} fill="var(--v-muted)">
                  {c.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} width={w} />
    </div>
  );
}

/* ── horizontal bars (ranked categories) ───────────────────────────────── */

/**
 * Ranked magnitude by category — one hue, because bar length already carries
 * the value; colouring bars by their own value would spend the identity
 * channel re-encoding what length shows.
 */
export function HBarChart({
  rows, format = "per1k", color = "var(--v-cat-1)",
}: {
  rows: { label: string; value: number | null; sub?: string; tipRows?: { label: string; value: string }[] }[];
  format?: ValueFormat;
  color?: string;
}) {
  const [tip, setTip] = useState<{ i: number } | null>(null);
  const vals = rows.map((r) => r.value).filter((v): v is number => v != null);
  if (!vals.length) return <EmptyChart message="No data for this selection." />;
  const max = Math.max(...vals, 0.000001);

  return (
    <div className="viz space-y-1">
      {rows.map((r, i) => (
        <div
          key={`${r.label}-${i}`}
          className="grid grid-cols-[minmax(96px,150px)_1fr_72px] items-center gap-3 rounded px-1 py-0.5 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
          onMouseEnter={() => setTip({ i })}
          onMouseLeave={() => setTip(null)}
          title={r.tipRows?.map((t) => `${t.label}: ${t.value}`).join(" · ")}
        >
          <div className="min-w-0">
            <div className="text-[11px] text-zinc-800 dark:text-zinc-200 truncate" title={r.label}>
              {r.label}
            </div>
            {r.sub && <div className="text-[10px] text-zinc-500 truncate">{r.sub}</div>}
          </div>
          <div className="h-3.5 rounded bg-zinc-100 dark:bg-zinc-800/60 overflow-hidden">
            <div
              className="h-full rounded-r-[4px] transition-[width] duration-200"
              style={{
                width: `${Math.max(0, Math.min(100, ((r.value ?? 0) / max) * 100))}%`,
                background: color,
                opacity: tip && tip.i !== i ? 0.55 : 1,
              }}
            />
          </div>
          <div className="text-[11px] text-right tabular-nums font-semibold text-zinc-800 dark:text-zinc-200">
            {fmtValue(r.value, format)}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── segmented quality bar (single row, 100%) ──────────────────────────── */

/** One horizontal 100% bar of the lead-quality mix, every tier direct-labelled. */
export function QualityMixBar({
  counts, height = 22,
}: {
  counts: Record<string, number>;
  height?: number;
}) {
  const total = QUALITY_TIERS.reduce((s, t) => s + (counts[t.key] ?? 0), 0);
  if (!total) return <EmptyChart message="No rated calls in this selection." />;
  return (
    <div className="viz">
      <div className="flex gap-[2px] w-full rounded overflow-hidden" style={{ height }}>
        {QUALITY_TIERS.map((t) => {
          const v = counts[t.key] ?? 0;
          if (!v) return null;
          return (
            <div
              key={t.key}
              className="flex items-center justify-center"
              style={{ width: `${(v / total) * 100}%`, background: t.color }}
              title={`${t.label}: ${v} (${((v / total) * 100).toFixed(1)}%)`}
            />
          );
        })}
      </div>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 mt-2">
        {QUALITY_TIERS.map((t) => {
          const v = counts[t.key] ?? 0;
          return (
            <li key={t.key} className="flex items-center gap-1.5">
              <span className="w-2.5 h-2.5 rounded-[2px]" style={{ background: t.color }} aria-hidden />
              <span className="text-[11px] text-zinc-600 dark:text-zinc-400">
                {t.label} <span className="tabular-nums font-semibold text-zinc-800 dark:text-zinc-200">{v}</span>
                <span className="text-zinc-500"> · {((v / total) * 100).toFixed(0)}%</span>
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ── data-table fallback ───────────────────────────────────────────────── */

/**
 * The table view every chart owes its reader — several palette steps sit below
 * 3:1 against the light surface, and the relief for that is visible labels or
 * this. Collapsed by default so it never competes with the chart.
 */
export function TableView({
  columns, rows, caption,
}: {
  columns: string[];
  rows: (string | number)[][];
  caption?: string;
}) {
  const [open, setOpen] = useState(false);
  useEffect(() => { /* keep state local; nothing to sync */ }, []);
  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-[11px] font-semibold text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
      >
        {open ? "▾" : "▸"} {open ? "Hide" : "Show"} data table
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full border-collapse text-[11px]">
            {caption && <caption className="sr-only">{caption}</caption>}
            <thead>
              <tr>
                {columns.map((c) => (
                  <th
                    key={c}
                    className="text-left px-2 py-1 font-semibold uppercase tracking-wider text-[9px] text-zinc-500 border-b border-zinc-200 dark:border-zinc-700"
                  >
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {r.map((cell, j) => (
                    <td
                      key={j}
                      className="px-2 py-1 tabular-nums text-zinc-700 dark:text-zinc-300 border-b border-zinc-100 dark:border-zinc-800/60 whitespace-nowrap"
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
