import type { CaseStatus, Tracking } from "../lib/api";

/**
 * Presentation-only pieces for the live journey.
 *
 * Every value shown here is derived from data the server already returned. Nothing in
 * this file fetches, decides, gates or records anything: the existing handlers, saved
 * state and approval rules in the live components remain the only authority.
 */

type Finding = NonNullable<CaseStatus["research"]>["findings"][number];
type PostalEvent = Tracking["events"][number];

const FINDING_META: Record<Finding["status"], { label: string; tone: string }> = {
  CONFIRMED: { label: "Confirmed", tone: "ok" },
  UNKNOWN: { label: "Not found", tone: "unk" },
  CONFLICTED: { label: "Conflicting", tone: "con" },
  NOT_APPLICABLE: { label: "Not applicable", tone: "na" },
};

const FINDING_ORDER: Finding["status"][] = ["CONFIRMED", "UNKNOWN", "CONFLICTED", "NOT_APPLICABLE"];

/**
 * Counts of this appeal's saved requirement findings, with an accessible legend.
 * The bar is decorative; the legend carries the literal numbers. No score, no
 * likelihood and no merits are expressed here.
 */
export function RequirementChecks({ findings, idPrefix }: { findings: Finding[]; idPrefix: string }) {
  const counts = FINDING_ORDER.map((status) => ({
    status,
    ...FINDING_META[status],
    count: findings.filter((finding) => finding.status === status).length,
  }));
  const total = counts.reduce((sum, entry) => sum + entry.count, 0);
  const titleId = `${idPrefix}RequirementChecksTitle`;
  return (
    <section className="req-checks" id={`${idPrefix}RequirementChecks`} aria-labelledby={titleId}>
      <div className="req-checks-head">
        <h3 id={titleId}>Requirement checks</h3>
        <p className="small muted">
          {total === 1 ? "1 requirement" : `${total} requirements`} recorded from this appeal&#39;s saved official pages.
          These are counts only, not a prediction or a judgement of your case.
        </p>
      </div>
      {total > 0 && (
        <div className="reqbar" aria-hidden="true">
          {counts.filter((entry) => entry.count > 0).map((entry) => (
            <span key={entry.status} className={`reqbar-seg tone-${entry.tone}`} style={{ flexGrow: entry.count }} />
          ))}
        </div>
      )}
      <ul className="reqbar-legend" aria-label="Requirement check counts">
        {counts.map((entry) => (
          <li key={entry.status} className={`tone-${entry.tone}`}>
            <span className="reqbar-dot" aria-hidden="true" />
            <b>{entry.count}</b> <span>{entry.label.toLowerCase()}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export interface PacketReviewProgressProps {
  adopted: boolean;
  changed: boolean;
  editingAdopted: boolean;
  /** Bytes exist for the current packet preview. Never the same as inspecting it. */
  previewReady: boolean;
  /** The existing inspection acknowledgement matched the current preview. */
  inspected: boolean;
  /** Submission pending or approval uncertain: the existing gates are in force. */
  locked: boolean;
  submissionPending: boolean;
  approvalUncertain: boolean;
}

/**
 * Edit → Adopt → Inspect → Approve, drawn only from the live letter's existing state.
 * Preview bytes alone never complete inspection: that stage needs the same acknowledgement
 * the approval button already requires.
 */
export function PacketReviewProgress({
  adopted,
  changed,
  editingAdopted,
  previewReady,
  inspected,
  locked,
  submissionPending,
  approvalUncertain,
}: PacketReviewProgressProps) {
  // Edit finishes when the saved text is neither changed nor being actively revised.
  // A saved but unadopted letter therefore lands on Adopt, while unsaved or revised
  // text stays on Edit. Inspection is only complete with the existing acknowledgement;
  // preview bytes alone never complete it.
  const editDone = !changed && !editingAdopted;
  const adoptDone = adopted && !editingAdopted;
  const inspectDone = adoptDone && inspected && previewReady;
  const ready = [editDone, adoptDone, inspectDone];
  const firstOpen = ready.findIndex((done) => !done);
  // A locked/uncertain submission has one active stage only: the approval check.
  const currentIndex = locked ? 3 : firstOpen === -1 ? 3 : firstOpen;

  const stageStates = ["Edit", "Adopt", "Inspect", "Approve"].map((label, index) => {
    if (index === 3 && locked) {
      return { label, state: "checking" as const, short: "Checking", text: submissionPending ? "Checking the saved mailing" : "Approval uncertain" };
    }
    if (index === 3) {
      return index === currentIndex
        ? { label, state: "current" as const, short: "Now", text: "" }
        : { label, state: "todo" as const, short: "Next", text: "" };
    }
    if (ready[index]) return { label, state: "done" as const, short: "Done", text: "" };
    if (index === currentIndex) return { label, state: "current" as const, short: "Now", text: "" };
    return { label, state: "todo" as const, short: "Next", text: "" };
  });

  const status = locked
    ? submissionPending
      ? "Your approval was submitted. We are checking the saved mailing record; do not approve or pay again."
      : approvalUncertain
        ? "The last approval attempt is uncertain. Check the saved status before approving again."
        : "Approval is paused while the saved mailing record is checked."
    : changed
      ? "Your edits are not saved and not adopted yet. Save, then adopt the exact text before inspecting the packet."
      : editingAdopted
        ? "You are editing adopted text. Adopt the revised text again before previewing the exact packet."
        : !adopted
          ? "Nothing is mailed until you adopt this exact text, inspect the assembled packet and approve it."
          : !previewReady
            ? "Your exact text is adopted. Preview your exact packet to inspect it."
            : !inspected
              ? "The preview is ready. Tick the inspection box after checking every page, the destination and your return address."
              : "Inspection recorded. You can approve this exact packet.";

  return (
    <nav className="packet-stages" id="livePacketProgress" aria-label="Packet review stages">
      <ol>
        {stageStates.map((stage, index) => (
          <li
            key={stage.label}
            className={`ps ps-${stage.state}`}
            aria-current={stage.state === "current" || stage.state === "checking" ? "step" : undefined}
          >
            <span className="ps-index" aria-hidden="true">{stage.state === "done" ? "✓" : index + 1}</span>
            <span className="ps-label">{stage.label}</span>
            <span className={`ps-state ps-${stage.state}`}>{stage.short}</span>
            <span className="sr">{stage.text}</span>
          </li>
        ))}
      </ol>
      <p className="ps-status">{status}</p>
    </nav>
  );
}

/**
 * The saved postal events, in the order the matching tracking record returned them.
 * Description text and timestamps are printed literally; nothing is simulated or added.
 */
export function PostalEventTrail({ events }: { events: PostalEvent[] }) {
  if (!events.length) return null;
  return (
    <ol className="postal-events" aria-label="Reported postal events">
      {events.map((event, index) => (
        <li key={`${event.event}-${event.occurred_at}-${index}`} className="postal-event">
          <span className="pe-marker" aria-hidden="true" />
          <div className="pe-body">
            <p className="pe-label">{event.event.replaceAll("_", " ")}</p>
            <p className="pe-desc">{event.description || event.event.replaceAll("_", " ")}</p>
            <time className="pe-time" dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString()}</time>
          </div>
        </li>
      ))}
    </ol>
  );
}
