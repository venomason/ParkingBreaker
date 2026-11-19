import { PHOTO_UPLOAD_GUIDANCE } from "../lib/api";
import type { EvidencePhotoEntry } from "./LiveDetails";

export function PhotoPicker({ id, label, camera = false, multiple = false, disabled = false, onPick }: {
  id: string;
  label: string;
  camera?: boolean;
  multiple?: boolean;
  disabled?: boolean;
  onPick: (files: File[]) => void;
}) {
  return <label className="btn photo-picker">
    {label}
    <input id={id} type="file" accept="image/jpeg,image/png" capture={camera ? "environment" : undefined}
      multiple={multiple} disabled={disabled} onChange={event => {
        const files = Array.from(event.currentTarget.files ?? []);
        event.currentTarget.value = "";
        if (files.length) onPick(files);
      }} />
  </label>;
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
  return <section className="card curbside-photos" id="scene-photos" tabIndex={-1} aria-labelledby="scene-title">
    <h3 id="scene-title">Scene photos (optional)</h3>
    <details className="guide-notes">
      <summary>Using your phone’s Panorama mode</summary>
      <p>From a safe spot, open Panorama in your phone’s Camera app and slowly turn. You can also take a series of overlapping photos.</p>
    </details>
    {live ? <>
      <div className="picker-row">
        <PhotoPicker id="sceneCamera" label="Take a scene photo" camera onPick={onAdd} />
        <PhotoPicker id="sceneLibrary" label="Choose photos" multiple onPick={onAdd} />
      </div>
      <details className="guide-notes"><summary>Accepted photo formats</summary><p>{PHOTO_UPLOAD_GUIDANCE}</p></details>
      <ul className="scene-list" aria-live="polite">
        {photos.map((entry, index) => <li key={entry.id}>
          <span>{entry.file?.name ?? `Scene photo ${index + 1}`}<small>{entry.error ?? (entry.ref ? "Uploaded" : "Selected")}</small></span>
          {entry.error && entry.file && <button type="button" className="chip" onClick={() => onRetry(entry)}>Retry</button>}
          <button type="button" className="chip" aria-label={`Remove scene photo ${index + 1}`} onClick={() => onRemove(entry.id)}>Remove</button>
        </li>)}
      </ul>
    </> : <>
      <button className="btn" id="sampleScene" type="button" aria-pressed={samplePhoto}
        onClick={() => onSamplePhoto(!samplePhoto)}>{samplePhoto ? "Remove sample sign photo" : "Add sample sign photo"}</button>
      {samplePhoto && <p role="status">Sample sign photo added to your appeal.</p>}
    </>}
  </section>;
}
