import { ContactsPage } from "@/components/contacts/ContactsPage";

type Tab = "assigned" | "used" | "all";

function parseTab(raw: string | string[] | undefined): Tab {
  const v = Array.isArray(raw) ? raw[0] : raw;
  return v === "all" || v === "used" || v === "assigned" ? v : "assigned";
}

function parseIds(raw: string | string[] | undefined): string[] {
  const v = Array.isArray(raw) ? raw[0] : raw;
  if (!v) return [];
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

export default async function ContactsGroup({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const sp = await searchParams;
  const ids = parseIds(sp.ids);
  const initialTab = parseTab(sp.tab);
  if (ids.length === 0) {
    return (
      <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 flex items-center justify-center">
        <p className="text-zinc-500">No assignments specified</p>
      </main>
    );
  }
  return <ContactsPage groupAssignmentIds={ids} initialTab={initialTab} />;
}
