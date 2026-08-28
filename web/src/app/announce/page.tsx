import { AnnounceForm } from "@/components/announce-form";
import { PageHeader } from "@/components/shell";

export default function AnnouncePage() {
  return (
    <>
      <PageHeader
        title="Tell the lab what is coming"
        meta="For sending sites, your manifest is checked against the study now, while the box is still on your bench"
      />
      <div className="mx-auto w-full max-w-5xl p-4 sm:p-6">
        <AnnounceForm />
      </div>
    </>
  );
}
