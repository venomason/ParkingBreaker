/**
 * ParkingBreaker Support — Thin Google Intake Forwarder.
 *
 * Reads Gmail messages (emails + Google Voice voicemail transcripts), extracts
 * the support code, and POSTs to the backend /internal/support/intake endpoint.
 * Sends ZERO emails itself and keeps ZERO copy of case data.
 */

function getOrCreateIntakeLabel(labelName) {
  return GmailApp.getUserLabelByName(labelName) || GmailApp.createLabel(labelName);
}

function applyIntakeLabel(thread, labelName) {
  if (!thread) return;
  thread.addLabel(getOrCreateIntakeLabel(labelName));
}

function postToBackendIntake(payload, cfg) {
  if (!/^https:\/\/[A-Za-z0-9.-]+(?::[0-9]{1,5})?\/internal\/support\/intake$/.test(cfg.backendIntakeUrl) ||
      !cfg.intakeSecret) {
    throw new Error('An HTTPS support intake URL and PB_SUPPORT_INTAKE_SECRET are required.');
  }
  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + cfg.intakeSecret
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
    followRedirects: false
  };
  var response;
  try {
    response = UrlFetchApp.fetch(cfg.backendIntakeUrl, options);
  } catch (e) {
    return { ok: false };
  }
  var status = response.getResponseCode();
  if (status < 200 || status >= 300) {
    return { ok: false, status: status };
  }
  try {
    var data = JSON.parse(response.getContentText());
    // Match the backend's SupportOutcome enum before treating a 2xx as delivered.
    if (!data || [
      'NO_ID_NO_RESPONSE', 'AUTO_REPLIED', 'FIXED_REPLIED', 'HELD_FOR_OWNER',
      'RATE_LIMITED_HELD', 'REJECTED_DRAFT_HELD', 'API_ERROR_HELD'
    ].indexOf(data.outcome) === -1) {
      return { ok: false, status: status };
    }
    return { ok: true, status: status, data: data };
  } catch (e) {
    return { ok: false, status: status };
  }
}

/**
 * Scheduled every 5 minutes via installIntakeTrigger().
 */
function pollSupportInbox() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) {
    return { skipped: true, reason: 'lock_busy' };
  }

  try {
    var cfg = getIntakeConfig();
    var query = 'in:inbox -in:spam -in:trash (is:unread OR -label:"' + PB_INTAKE_CONFIG.LABELS.PROCESSED + '")';
    var threads = GmailApp.search(query, 0, cfg.maxThreadsPerRun);

    var summary = {
      threadsScanned: threads.length,
      forwarded: 0,
      autoReplied: 0,
      heldForOwner: 0,
      noIdNoResponse: 0,
      ignored: 0,
      deliveryErrors: 0
    };

    for (var t = 0; t < threads.length; t++) {
      var thread = threads[t];
      var messages = thread.getMessages();

      for (var m = 0; m < messages.length; m++) {
        var rawMsg = messages[m];
        if (typeof rawMsg.isUnread === 'function' && !rawMsg.isUnread() &&
            thread.getLabels().some(function (l) { return l.getName() === PB_INTAKE_CONFIG.LABELS.PROCESSED; })) {
          continue;
        }

        var parsed = parseInboundForBackend(rawMsg, cfg);
        if (parsed.ignoreReason) {
          if (parsed.ignoreReason !== 'IGNORED_SELF_SENT') {
            applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.IGNORED);
            applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.PROCESSED);
          }
          if (typeof rawMsg.markRead === 'function') rawMsg.markRead();
          summary.ignored++;
          continue;
        }

        var res = postToBackendIntake(parsed.payload, cfg);
        if (!res.ok) {
          applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.DELIVERY_ERROR);
          rawMsg.markUnread();
          summary.deliveryErrors++;
          continue;
        }

        summary.forwarded++;
        var outcome = res.data && res.data.outcome ? String(res.data.outcome) : '';
        if (outcome === 'NO_ID_NO_RESPONSE') {
          applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.NO_ID_NO_RESPONSE);
          summary.noIdNoResponse++;
        } else if (outcome === 'AUTO_REPLIED' || outcome === 'FIXED_REPLIED') {
          applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.AUTO_REPLIED);
          summary.autoReplied++;
        } else {
          applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.HELD_FOR_OWNER);
          summary.heldForOwner++;
        }
        applyIntakeLabel(thread, PB_INTAKE_CONFIG.LABELS.PROCESSED);
        if (typeof rawMsg.markRead === 'function') rawMsg.markRead();
      }
    }

    Logger.log('pollSupportInbox complete: ' + JSON.stringify(summary));
    return summary;
  } finally {
    lock.releaseLock();
  }
}

function installIntakeTrigger(pollIntervalMinutes) {
  var minutes = pollIntervalMinutes || 5;
  ScriptApp.getProjectTriggers().forEach(function (trig) {
    if (trig.getHandlerFunction() === 'pollSupportInbox') {
      ScriptApp.deleteTrigger(trig);
    }
  });
  ScriptApp.newTrigger('pollSupportInbox')
    .timeBased()
    .everyMinutes(minutes)
    .create();
  return { pollIntervalMinutes: minutes };
}

function removeIntakeTrigger() {
  var removed = 0;
  ScriptApp.getProjectTriggers().forEach(function (trig) {
    if (trig.getHandlerFunction() === 'pollSupportInbox') {
      ScriptApp.deleteTrigger(trig);
      removed++;
    }
  });
  return { removed: removed };
}
