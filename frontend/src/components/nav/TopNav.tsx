"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ThemeToggle } from "./ThemeToggle";

const NAV_LINKS = [
  { href: "/", label: "Planning" },
  { href: "/copy-generator", label: "Copy Generator" },
  { href: "/upload", label: "List Upload" },
  { href: "/library", label: "Library" },
  { href: "/statistics", label: "Statistics" },
  { href: "/blocklist", label: "Blocklist" },
  { href: "/connectors", label: "Connectors" },
  { href: "/sync", label: "Sync" },
  { href: "/api-docs", label: "API Docs" },
];

export function TopNav() {
  const pathname = usePathname();

  function isActive(href: string) {
    if (href === "/") return pathname === "/" || pathname === "/planning";
    return pathname.startsWith(href);
  }

  return (
    <header className="sticky top-0 z-50 bg-white dark:bg-zinc-950/80 backdrop-blur-md border-b border-zinc-200 dark:border-zinc-800/40">
      <div className="px-6 h-12 flex items-center gap-3">
        <div className="flex items-center gap-2 select-none">
          <div className="w-[18px] h-[18px] rounded bg-zinc-100 flex items-center justify-center shrink-0">
            <svg width="9" height="9" viewBox="0 0 10 10" fill="none">
              <path d="M2 5h6M5 2l3 3-3 3" stroke="#09090b" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <span className="text-[13px] font-semibold text-zinc-800 dark:text-zinc-300 tracking-tight">Webinar Studio</span>
        </div>

        <div className="w-px h-3 bg-zinc-100 dark:bg-zinc-800" />

        <nav className="flex items-center gap-0.5">
          {NAV_LINKS.map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              className={`px-2.5 py-1 rounded text-[13px] font-medium transition-colors duration-100 ${
                isActive(href)
                  ? "bg-zinc-100 dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100"
                  : "text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
              }`}
            >
              {label}
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex items-center">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
