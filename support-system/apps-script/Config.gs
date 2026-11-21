/**
 * ParkingBreaker Support — Thin Google Intake Layer Configuration.
 *
 * No Google Sheets database. No outbound email sending. Apps Script only reads
 * Gmail (including Google Voice voicemail transcripts), redacts card numbers,
 * extracts the PB-XXXXX support code, and POSTs to the backend /internal/support/intake.
 */

var PB_INTAKE_CONFIG = {
  TIMEZONE: 'America/Los_Angeles',
  LABELS: {
    PROCESSED: 'PB-Support/Processed',
    AUTO_REPLIED: 'PB-Support/Auto-Replied',
    HELD_FOR_OWNER: 'PB-Support/Held-For-Owner',
    NO_ID_NO_RESPONSE: 'PB-Support/No-ID-No-Response',
    IGNORED: 'PB-Support/Ignored',
    DELIVERY_ERROR: 'PB-Support/Intake-Retry'
  },
  DEFAULTS: {
    MAX_THREADS_PER_RUN: 20
  },
  GOOGLE_VOICE_SENDERS: [
    'voice-noreply@google.com'
  ]
};

function getIntakeConfig() {
  var props = PropertiesService.getScriptProperties();
  var all = props ? props.getProperties() : {};

  function getStr(key, fallback) {
    var val = all[key];
    return val !== undefined && String(val).trim() !== '' ? String(val).trim() : fallback;
  }

  function getNum(key, fallback) {
    var raw = all[key];
    if (raw === undefined || String(raw).trim() === '') return fallback;
    var parsed = Number(raw);
    return isNaN(parsed) ? fallback : parsed;
  }

  return {
    backendIntakeUrl: getStr('PB_BACKEND_INTAKE_URL', ''),
    intakeSecret: getStr('PB_SUPPORT_INTAKE_SECRET', ''),
    supportEmail: getStr('SUPPORT_EMAIL', '').toLowerCase(),
    maxThreadsPerRun: getNum('MAX_THREADS_PER_RUN', PB_INTAKE_CONFIG.DEFAULTS.MAX_THREADS_PER_RUN)
  };
}
