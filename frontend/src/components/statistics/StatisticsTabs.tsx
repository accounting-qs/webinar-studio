"use client";

import { useState } from "react";
import { StatisticsPage } from "./StatisticsPage";
import { SegmentsTab } from "./SegmentsTab";
import { ListSourceTab } from "./ListSourceTab";
import { CalendarUploadsTab } from "./CalendarUploadsTab";
import { BookingCalendarsTab } from "./BookingCalendarsTab";
import { AccountHealthTab } from "./AccountHealthTab";
import { DayOfWeekTab } from "./DayOfWeekTab";

type Tab = "statistics" | "segments" | "by-list-source" | "calendar-uploads" | "booking-calendars" | "account-health" | "send-day";

export function StatisticsTabs() {
  const [tab, setTab] = useState<Tab>("statistics");

  return (
    <div className="flex flex-col" style={{ height: "calc(100vh - 0px)" }}>
      <div className="flex-none border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 px-6">
        <div className="flex gap-1">
          <TabButton active={tab === "statistics"} onClick={() => setTab("statistics")}>
            Statistics
          </TabButton>
          <TabButton active={tab === "segments"} onClick={() => setTab("segments")}>
            Segments
          </TabButton>
          <TabButton active={tab === "by-list-source"} onClick={() => setTab("by-list-source")}>
            By List Source
          </TabButton>
          <TabButton active={tab === "account-health"} onClick={() => setTab("account-health")}>
            Account Health
          </TabButton>
          <TabButton active={tab === "send-day"} onClick={() => setTab("send-day")}>
            Send Day
          </TabButton>
          <TabButton active={tab === "calendar-uploads"} onClick={() => setTab("calendar-uploads")}>
            Calendar Uploads
          </TabButton>
          <TabButton active={tab === "booking-calendars"} onClick={() => setTab("booking-calendars")}>
            Calendars
          </TabButton>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-auto">
        {tab === "statistics" ? (
          <StatisticsPage />
        ) : tab === "segments" ? (
          <SegmentsTab />
        ) : tab === "by-list-source" ? (
          <ListSourceTab />
        ) : tab === "calendar-uploads" ? (
          <CalendarUploadsTab />
        ) : tab === "booking-calendars" ? (
          <BookingCalendarsTab />
        ) : tab === "account-health" ? (
          <AccountHealthTab />
        ) : (
          <DayOfWeekTab />
        )}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-xs font-semibold border-b-2 -mb-px transition-colors ${
        active
          ? "border-violet-500 text-violet-500"
          : "border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
      }`}
    >
      {children}
    </button>
  );
}
