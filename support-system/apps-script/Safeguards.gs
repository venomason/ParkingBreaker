/**
 * ParkingBreaker Support — Thin Intake Safeguards & Card Redaction.
 *
 * Scrubs payment card numbers, CVVs, SSNs, and API/bearer tokens BEFORE anything
 * leaves Google Workspace, and filters out bounces, OOF, mailing lists, self-sent
 * messages, and Google Voice SMS notifications (strict no-SMS design).
 */

function normalizeEmail(raw) {
  if (!raw) return '';
  var str = String(raw).trim();
  var angle = str.match(/<([^<>]+)>/);
  var addr = (angle ? angle[1] : str).trim().toLowerCase();
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(addr) ? addr : '';
}

function passesLuhnCheck(digits) {
  if (!/^\d{13,19}$/.test(digits)) return false;
  var sum = 0;
  var shouldDouble = false;
  for (var i = digits.length - 1; i >= 0; i--) {
    var d = parseInt(digits.charAt(i), 10);
    if (shouldDouble) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    shouldDouble = !shouldDouble;
  }
  return sum % 10 === 0;
}

function redactSensitiveData(input) {
  if (input === null || input === undefined) return '';
  var text = String(input);
  text = text.replace(/\bBearer\s+[A-Za-z0-9\-._~+/]+=*/gi, 'Bearer [REDACTED_TOKEN]');
  text = text.replace(/\b(?:sk_live|sk_test|pk_live|pk_test|rk_live|rk_test|whsec)_[A-Za-z0-9]{10,}\b/g, '[REDACTED_KEY]');
  text = text.replace(/\bAIza[0-9A-Za-z\-_]{20,}\b/g, '[REDACTED_API_KEY]');
  text = text.replace(/\b\d{3}-\d{2}-\d{4}\b/g, '[REDACTED_SSN]');
  text = text.replace(/\b(cvv|cvc|cid|security\s+code|card\s+code)(\s*(?:is|:|#|-))?\s*\d{3,4}\b/gi, '$1 [REDACTED_CVV]');
  text = text.replace(/(^|[^\d])((?:\d[ -]?){12,18}\d)(?!\d)/g, function (full, prefix, candidate) {
    var digitsOnly = candidate.replace(/\D/g, '');
    if (digitsOnly.length >= 13 && digitsOnly.length <= 19 && passesLuhnCheck(digitsOnly)) {
      return prefix + '[REDACTED_CARD]';
    }
    return full;
  });
  return text;
}

function getSafeHeader(rawMsg, headerName) {
  if (!rawMsg || typeof rawMsg.getHeader !== 'function') return '';
  var val = rawMsg.getHeader(headerName) || rawMsg.getHeader(headerName.toLowerCase());
  return val ? String(val).trim() : '';
}

function isGoogleVoiceSmsNotification(rawMsg) {
  var fromEmail = normalizeEmail(rawMsg.getFrom ? rawMsg.getFrom() : '');
  var subject = String(rawMsg.getSubject ? rawMsg.getSubject() : '').trim();
  if (fromEmail.indexOf('txt.voice.google.com') !== -1) return true;
  if (PB_INTAKE_CONFIG.GOOGLE_VOICE_SENDERS.indexOf(fromEmail) !== -1 &&
      /^new text message from\b/i.test(subject)) {
    return true;
  }
  return false;
}

function getAutoIgnoreReason(rawMsg, cfg) {
  var fromEmail = normalizeEmail(rawMsg.getFrom ? rawMsg.getFrom() : '');
  var subject = String(rawMsg.getSubject ? rawMsg.getSubject() : '').trim();
  if (!fromEmail) return 'IGNORED_MISSING_SENDER';
  if (cfg.supportEmail && fromEmail === cfg.supportEmail) return 'IGNORED_SELF_SENT';
  if (/^\[ParkingBreaker Support\b/i.test(subject)) return 'IGNORED_INTERNAL_NOTIFICATION';
  if (isGoogleVoiceSmsNotification(rawMsg)) return 'IGNORED_SMS_NOT_SUPPORTED';

  var autoSubmitted = getSafeHeader(rawMsg, 'Auto-Submitted').toLowerCase();
  if (autoSubmitted && autoSubmitted !== 'no') return 'IGNORED_AUTO_SUBMITTED_HEADER';

  var precedence = getSafeHeader(rawMsg, 'Precedence').toLowerCase();
  if (/^(bulk|junk|list|auto_reply|auto-reply)$/.test(precedence)) return 'IGNORED_BULK_PRECEDENCE';

  var autoSuppress = getSafeHeader(rawMsg, 'X-Auto-Response-Suppress').toLowerCase();
  if (autoSuppress && /(all|oof|autoreply|dr)/.test(autoSuppress)) return 'IGNORED_AUTO_RESPONSE_SUPPRESS';

  if (getSafeHeader(rawMsg, 'List-Id') || getSafeHeader(rawMsg, 'List-Unsubscribe')) {
    return 'IGNORED_MAILING_LIST';
  }
  if (/^(mailer-daemon|postmaster|noreply|no-reply|donotreply|do-not-reply|bounces?|bounce-.*|autoresponder|notifications?)@/i.test(fromEmail)) {
    return 'IGNORED_AUTOMATED_SENDER';
  }
  if (/^(automatic reply|out of office|auto:|undeliverable|delivery status notification|mail delivery subsystem|returned mail|failure notice)\b/i.test(subject)) {
    return 'IGNORED_BOUNCE_OR_OOF_SUBJECT';
  }
  return '';
}
