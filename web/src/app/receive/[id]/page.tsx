import { ReceivingBench } from "@/components/receiving-bench";

export default async function ReceiveCasePage({ params }: PageProps<"/receive/[id]">) {
  const { id } = await params;
  return <ReceivingBench caseId={id} />;
}
