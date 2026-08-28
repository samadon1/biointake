import { DecisionCard } from "@/components/decision-card";

export default async function DecidePage(props: PageProps<"/cases/[id]/decide/[interruptId]">) {
  const { id, interruptId } = await props.params;
  return <DecisionCard caseId={id} interruptId={decodeURIComponent(interruptId)} />;
}
