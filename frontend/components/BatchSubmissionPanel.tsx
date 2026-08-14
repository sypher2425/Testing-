import Link from "next/link";

export type BatchSubmissionStatus = "waiting" | "submitting" | "submitted" | "failed";

export interface BatchSubmissionItem {
  key: string;
  label: string;
  status: BatchSubmissionStatus;
  progress: number;
  jobId?: string;
  error?: string;
}

export default function BatchSubmissionPanel({
  items,
  active,
  onNewBatch,
}: {
  items: BatchSubmissionItem[];
  active: boolean;
  onNewBatch: () => void;
}) {
  const submitted = items.filter((item) => item.status === "submitted").length;
  const failed = items.filter((item) => item.status === "failed").length;
  const complete = submitted + failed;
  const overall = items.length ? Math.round(items.reduce((sum, item) => sum + item.progress, 0) / items.length) : 0;

  return (
    <div className="batch-panel card" aria-live="polite">
      <div className="batch-summary">
        <div>
          <span className="batch-kicker">Batch submission</span>
          <h4>{active ? `Submitting ${Math.min(complete + 1, items.length)} of ${items.length}` : `${submitted} of ${items.length} queued`}</h4>
          <p>
            {failed > 0
              ? `${failed} item${failed === 1 ? "" : "s"} need attention. Successful jobs remain queued.`
              : active
                ? "Sources are validated and queued as each submission completes."
                : "Every source was accepted and added to the processing queue."}
          </p>
        </div>
        <strong>{overall}%</strong>
      </div>

      <div className="progress-track">
        <div className="progress-value" style={{ width: `${overall}%` }} />
      </div>

      <ul className="batch-items">
        {items.map((item) => (
          <li key={item.key} className={`batch-item batch-item-${item.status}`}>
            <span className="batch-state" aria-hidden="true">
              {item.status === "submitted" ? "✓" : item.status === "failed" ? "!" : item.status === "submitting" ? "↗" : "·"}
            </span>
            <div className="min-w-0">
              <p title={item.label}>{item.label}</p>
              <small>
                {item.status === "waiting" && "Waiting to submit"}
                {item.status === "submitting" && `Submitting · ${item.progress}%`}
                {item.status === "failed" && item.error}
                {item.status === "submitted" && "Queued successfully"}
              </small>
            </div>
            {item.jobId && (
              <Link href={`/jobs/${item.jobId}`} className="batch-job-link">
                View
              </Link>
            )}
          </li>
        ))}
      </ul>

      {!active && (
        <button type="button" className="btn-secondary mt-4" onClick={onNewBatch}>
          Start a new batch
        </button>
      )}
    </div>
  );
}
