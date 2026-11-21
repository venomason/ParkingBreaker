/**
 * Validates and normalizes an authoritative backend-issued ParkingBreaker
 * spoken support code (PB-XXXXX). Never invents or hashes a code from an
 * opaque internal case_id.
 */
export function formatSupportCaseId(rawCode?: string | null): string | null {
  const trimmed = (rawCode ?? "").trim().toUpperCase();
  if (!trimmed) return null;
  const match = trimmed.match(/^PB-(\d{5})$/);
  return match ? `PB-${match[1]}` : null;
}
