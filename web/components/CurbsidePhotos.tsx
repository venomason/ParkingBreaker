import { Icon } from "./Icons";
import type { EvidencePhotoEntry } from "./LiveDetails";
import { PHOTO_UPLOAD_GUIDANCE } from "../lib/api";

export function PhotoPicker({ id, label, camera = false, multiple = false, disabled = false, primary = false, onPick }: {
  id: string;
  label: string;
  camera?: boolean;
  multiple?: boolean;
  disabled?: boolean;
  primary?: boolean;
  onPick: (files: File[]) => void;
}) {
  return (
    <label className={`btn ${primary ? "btn-primary" : "btn-ghost"} photo-picker${disabled ? " is-disabled" : ""}`}>
      <Icon id={camera ? "camera" : "doc"} size={16} />
      <span>{label}</span>
      <input
        id={id}
        type="file"
        accept="image/jpeg,image/png"
        capture={camera ? "environment" : undefined}
        multiple={multiple}
        disabled={disabled}
        onChange={event => {
          const files = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          if (files.length) onPick(files);
        }}
      />
    </label>
  );
}

type Props = {
  live: boolean;
  samplePhoto: boolean;
  onSamplePhoto: (selected: boolean) => void;
  photos: EvidencePhotoEntry[];
  onAdd: (files: File[]) => void;
  onRemove: (id: string) => void;
  onRetry: (entry: EvidencePhotoEntry) => void;
};

export function CurbsidePhotos({ live, samplePhoto, onSamplePhoto, photos, onAdd, onRemove, onRetry }: Props) {
  return (
    <section className="card curbside-photos" id="scene-photos" tabIndex={-1} aria-labelledby="scene-title">
      <div className="section-head-row">
        <div>
          <p className="eyebrow">Optional supporting evidence</p>
          <h3 id="scene-title">Scene photos (optional)</h3>
        </div>
        {live && photos.length > 0 && (
          <span className="status-pill is-saved">
            <Icon id="check" size={11} /> {photos.length} {photos.length === 1 ? "photo" : "photos"}
          </span>
        )}
      </div>
      <p className="small muted">
        Signs, meter screens, curb markings, or parking receipts from around your car. You can add or remove these before mailing.
      </p>
      {live ? (
        <>
          <div className="picker-row">
            <PhotoPicker id="sceneCamera" label="Take a scene photo" camera onPick={onAdd} />
            <PhotoPicker id="sceneLibrary" label="Choose photos" multiple onPick={onAdd} />
          </div>
          <details className="guide-notes">
            <summary>Panorama &amp; accepted photo formats</summary>
            <p>From a safe spot, open Panorama in your phone’s Camera app and slowly turn, or take overlapping photos. {PHOTO_UPLOAD_GUIDANCE}</p>
          </details>
          {photos.length > 0 && (
            <ul className="scene-list" aria-live="polite">
              {photos.map((entry, index) => (
                <li key={entry.id} className={entry.error ? "is-attention" : entry.ref ? "is-saved" : undefined}>
                  <span>
                    <strong>{entry.file?.name ?? `Scene photo ${index + 1}`}</strong>
                    <small className={entry.error ? "err-inline" : undefined}>
                      {entry.error ?? (entry.ref ? "✓ Saved for this appeal" : "Uploading…")}
                    </small>
                  </span>
                  {entry.error && entry.file && (
                    <button type="button" className="chip" onClick={() => onRetry(entry)}>
                      Retry
                    </button>
                  )}
                  <button
                    type="button"
                    className="chip"
                    aria-label={`Remove scene photo ${index + 1}`}
                    onClick={() => onRemove(entry.id)}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <>
          <button
            className="btn btn-ghost"
            id="sampleScene"
            type="button"
            aria-pressed={samplePhoto}
            onClick={() => onSamplePhoto(!samplePhoto)}
          >
            {samplePhoto ? "Remove sample sign photo" : "Add sample sign photo"}
          </button>
          {samplePhoto && (
            <p className="status-banner is-saved" role="status">
              <Icon id="check" size={16} /> Sample sign photo added to your appeal.
            </p>
          )}
        </>
      )}
    </section>
  );
}
