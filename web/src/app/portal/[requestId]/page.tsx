import { SenderPortal } from "@/components/sender-portal";

export default async function PortalPage(props: PageProps<"/portal/[requestId]">) {
  const { requestId } = await props.params;
  const sp = await props.searchParams;
  const token = typeof sp.token === "string" ? sp.token : "";
  return <SenderPortal requestId={requestId} token={token} />;
}
