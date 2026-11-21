import { OutcomeLink } from "../../../components/OutcomeLink";

export default async function OutcomeLinkPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const caseId = typeof params.case === "string" ? params.case : "";
  const token = typeof params.token === "string" ? params.token : "";
  return <OutcomeLink caseId={caseId} token={token} />;
}
