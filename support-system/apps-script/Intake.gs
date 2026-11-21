/**
 * ParkingBreaker Support — Thin Intake Parser (Gmail & Google Voice Voicemails).
 *
 * Extracts ONLY: channel ('email' | 'voicemail'), redacted question text,
 * canonical 5-digit support code (PB-XXXXX or null), message ID, and timestamp.
 * Never looks up cases by phone number or email address.
 */

function isGoogleVoiceVoicemail(rawMsg) {
  var fromEmail = normalizeEmail(rawMsg.getFrom ? rawMsg.getFrom() : '');
  var subject = String(rawMsg.getSubject ? rawMsg.getSubject() : '').trim();
  return PB_INTAKE_CONFIG.GOOGLE_VOICE_SENDERS.indexOf(fromEmail) !== -1 &&
    /^new voicemail from\b/i.test(subject);
}

/**
 * Extracts the first clear spoken or written 5-digit PB-XXXXX support code.
 * Rejects garbled, short, or malformed IDs (returns null).
 */
function extractSupportCode(text) {
  if (!text) return null;
  var str = String(text);

  var spoken = /\bP\.?\s*B\.?\s*(?:(?:-|–|—|\bdash\b|\bhyphen\b)\s*)?(\d(?:[ \t-]?\d){4})\b/i.exec(str);
  if (spoken) {
    var digits = spoken[1].replace(/\D/g, '');
    if (digits.length === 5) return 'PB-' + digits;
  }

  var compact = /\bPB(\d{5})\b/i.exec(str);
  if (compact) {
    return 'PB-' + compact[1];
  }

  return null;
}

function parseGoogleVoiceTranscript(body) {
  var text = String(body || '').replace(/\r\n/g, '\n').replace(/<https?:\/\/[^>]+>/gi, '');
  var lines = text.split('\n');
  var kept = [];

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (/^(play message|play voicemail|listen to voicemail|your account|help center|google voice|google llc|to respond to|you received this email because)/i.test(line)) {
      break;
    }
    if (!line) {
      if (kept.length > 0) kept.push('');
      continue;
    }
    if (/^(new voicemail from|voicemail from|transcript:?|message:?|from:|date:|duration:|https?:\/\/voice\.google\.com)/i.test(line)) {
      var inline = line.replace(/^transcript:\s*/i, '');
      if (/^transcript:/i.test(line) && inline) kept.push(inline);
      continue;
    }
    kept.push(line);
  }

  var transcript = kept.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  if (/^(transcript\s+(?:not|un)available|no transcript available|unable to transcribe|\(no transcript\))\.?$/i.test(transcript)) {
    return '';
  }
  return transcript;
}

function stripQuotedEmailText(body) {
  var text = String(body || '').replace(/\r\n/g, '\n');
  var lines = text.split('\n');
  var result = [];

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    var trimmed = line.trim();
    if (/^on .+ wrote:$/i.test(trimmed)) break;
    if (/^-{2,}\s*(?:original|forwarded) message\s*-{2,}$/i.test(trimmed)) break;
    if (/^from:\s+.+@.+$/i.test(trimmed) && i > 0) break;
    if (line === '-- ' || trimmed === '--') break;
    if (/^>/.test(trimmed)) continue;
    result.push(line);
  }

  return result.join('\n').replace(/\n{3,}/g, '\n\n').trim() || text.trim();
}

function parseInboundForBackend(rawMsg, cfg) {
  var messageId = rawMsg.getId ? String(rawMsg.getId()) : '';
  var dateObj = rawMsg.getDate ? rawMsg.getDate() : new Date();
  var receivedAt = dateObj instanceof Date ? dateObj.toISOString() : new Date().toISOString();
  var subject = String(rawMsg.getSubject ? rawMsg.getSubject() : '').trim();
  var plainBody = String(rawMsg.getPlainBody ? rawMsg.getPlainBody() : '').trim();

  if (isGoogleVoiceVoicemail(rawMsg)) {
    var vmText = redactSensitiveData(parseGoogleVoiceTranscript(plainBody));
    return {
      ignoreReason: '',
      payload: {
        message_id: messageId,
        channel: 'voicemail',
        text: vmText,
        support_code: extractSupportCode(vmText),
        received_at: receivedAt
      }
    };
  }

  var ignoreReason = getAutoIgnoreReason(rawMsg, cfg);
  var emailText = redactSensitiveData(stripQuotedEmailText(plainBody));
  var combined = redactSensitiveData(subject) + '\n' + emailText;

  return {
    ignoreReason: ignoreReason,
    payload: {
      message_id: messageId,
      channel: 'email',
      text: emailText,
      support_code: extractSupportCode(combined),
      received_at: receivedAt
    }
  };
}
