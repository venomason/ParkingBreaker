// Development only: advances a case's worker steps (research, draft, dispatch) the way the
// scheduler does in production. Off unless PB_DEV_WORKER=1; the secret stays on the server.
const STEPS = new Set(["research", "draft", "dispatch"]);

export async function POST(
  _request: Request,
  context: { params: Promise<{ caseId: string; step: string }> },
): Promise<Response> {
  const { caseId, step } = await context.params;
  const secret = process.env.PB_WORKER_SECRET ?? "";
  if (process.env.NODE_ENV !== "development" || process.env.PB_DEV_WORKER !== "1" || !secret || !STEPS.has(step) || !/^case_[a-f0-9]+$/.test(caseId)) {
    return Response.json({ detail: "not_available" }, { status: 404 });
  }
  const origin = process.env.PB_API_ORIGIN || "http://127.0.0.1:8000";
  const response = await fetch(`${origin}/internal/cases/${caseId}/step`, {
    method: "POST",
    headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/json" },
    body: JSON.stringify({ step_id: step }),
  });
  return new Response(await response.text(), {
    status: response.status,
    headers: { "Content-Type": "application/json" },
  });
}
