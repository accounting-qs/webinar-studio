import { WebinarReportPage } from "@/components/statistics/WebinarReportPage";

export default async function Report({
  params,
}: {
  params: Promise<{ webinarId: string }>;
}) {
  const { webinarId } = await params;
  return <WebinarReportPage webinarId={webinarId} />;
}
