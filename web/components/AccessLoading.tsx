/** Presentation only: callers retain their existing access and recovery gates. */
export function AccessLoading({ title = "Getting your appeal ready", message = "Loading… Checking the service for this appeal." }: { title?: string; message?: string }) {
  return (
    <div className="access-loading">
      <p className="access-eyebrow">Your private appeal</p>
      <h1>{title}</h1>
      <p role="status" aria-live="polite">{message}</p>
      <div className="access-skeleton" aria-hidden="true">
        <span className="skeleton-line" /><span className="skeleton-line short" />
        <span className="skeleton-field" /><span className="skeleton-field" />
        <span className="skeleton-action" />
      </div>
    </div>
  );
}
