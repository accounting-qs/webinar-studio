"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS: { href: string; label: string }[] = [
  { href: "/statistics/home", label: "Home" },
  { href: "/statistics", label: "Statistics" },
  { href: "/statistics/segments", label: "Segments" },
  { href: "/statistics/by-list-source", label: "By List Source" },
  { href: "/statistics/employee-count", label: "Employee count" },
  { href: "/statistics/account-health", label: "Account Health" },
  { href: "/statistics/send-day", label: "Send Day" },
  { href: "/statistics/calendar-uploads", label: "Calendar Uploads" },
  { href: "/statistics/calendars", label: "Calendars" },
];

export default function StatisticsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="flex flex-col" style={{ height: "calc(100vh - 0px)" }}>
      <div className="flex-none border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 px-6">
        <div className="flex gap-1">
          {TABS.map((t) => (
            <TabLink key={t.href} href={t.href} active={pathname === t.href}>
              {t.label}
            </TabLink>
          ))}
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-auto">{children}</div>
    </div>
  );
}

function TabLink({
  href,
  active,
  children,
}: {
  href: string;
  active: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className={`px-3 py-2 text-xs font-semibold border-b-2 -mb-px transition-colors ${
        active
          ? "border-violet-500 text-violet-500"
          : "border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
      }`}
    >
      {children}
    </Link>
  );
}
