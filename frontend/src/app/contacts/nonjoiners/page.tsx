import { ContactsPage } from "@/components/contacts/ContactsPage";

function parseId(raw: string | string[] | undefined): string {
  const v = Array.isArray(raw) ? raw[0] : raw;
  return (v || "").trim();
}

export default async function ContactsNonjoiners({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const sp = await searchParams;
  const webinarId = parseId(sp.webinar);
  if (!webinarId) {
    return (
      <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 flex items-center justify-center">
        <p className="text-zinc-500">No webinar specified</p>
      </main>
    );
  }
  return <ContactsPage nonjoinerWebinarId={webinarId} />;
}
