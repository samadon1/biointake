import { Suspense } from "react";
import { CaseWorkspace } from "@/components/case-workspace";

export default async function CasePage(props: PageProps<"/cases/[id]">) {
  const { id } = await props.params;
  return (
    <Suspense>
      <CaseWorkspace caseId={id} />
    </Suspense>
  );
}
