import { LabConfiguration } from "@/components/lab-configuration";
import { PageHeader } from "@/components/shell";

export default function LabPage() {
  return (
    <>
      <PageHeader
        title="Lab configuration"
        meta="The sites this lab writes to, and the studies it receives against, both in place before any box arrives"
      />
      <div className="mx-auto w-full max-w-5xl p-4 sm:p-6">
        <LabConfiguration />
      </div>
    </>
  );
}
