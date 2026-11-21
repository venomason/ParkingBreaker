'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APPS_SCRIPT_DIR = path.resolve(__dirname, '../apps-script');

function createThinIntakeSandbox({ urlFetchHandler, scriptProperties = {} } = {}) {
  const sentEmails = [];
  const drafts = [];
  const fetchCalls = [];
  const labels = new Map();
  const threads = [];
  const triggers = [];

  const props = {
    PB_BACKEND_INTAKE_URL: 'https://api.parkingbreaker.example/internal/support/intake',
    PB_SUPPORT_INTAKE_SECRET: 'test-intake-secret-0123456789',
    SUPPORT_EMAIL: 'support@parkingbreaker.com',
    ...scriptProperties
  };

  function makeLabel(name) {
    if (!labels.has(name)) labels.set(name, { getName: () => name });
    return labels.get(name);
  }

  function addThread({ id, messages, isSpam = false, isTrash = false }) {
    const lblSet = new Set();
    const msgObjs = messages.map((m) => {
      let unread = m.unread !== undefined ? m.unread : true;
      const hdrs = {};
      for (const [k, v] of Object.entries(m.headers || {})) hdrs[k.toLowerCase()] = v;
      return {
        getId: () => m.id,
        getFrom: () => m.from,
        getSubject: () => m.subject || '',
        getPlainBody: () => m.body || '',
        getDate: () => (m.date ? new Date(m.date) : new Date('2026-11-20T18:00:00Z')),
        getHeader: (k) => hdrs[String(k).toLowerCase()] || '',
        isUnread: () => unread,
        markRead: () => { unread = false; },
        markUnread: () => { unread = true; }
      };
    });
    const thread = {
      id,
      isSpam,
      isTrash,
      _labels: lblSet,
      getId: () => id,
      getMessages: () => msgObjs,
      getLabels: () => Array.from(lblSet).map((n) => makeLabel(n)),
      addLabel: (l) => lblSet.add(l.getName()),
      isUnread: () => msgObjs.some((m) => m.isUnread()),
      reply: () => { sentEmails.push('FORBIDDEN_REPLY'); throw new Error('Thin intake must never send email'); },
      createDraftReply: () => { drafts.push('FORBIDDEN_DRAFT'); throw new Error('Thin intake must never create drafts'); }
    };
    threads.push(thread);
    return thread;
  }

  const sandbox = {
    Date, Math, JSON, RegExp, Error, Number, String, Boolean, Array, Object, parseInt, isNaN,
    Logger: { log: () => {} },
    PropertiesService: {
      getScriptProperties: () => ({ getProperties: () => ({ ...props }) })
    },
    LockService: {
      getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} })
    },
    // Sheets MUST NOT be used anywhere in the thin intake layer
    SpreadsheetApp: {
      openById: () => { throw new Error('Thin intake must never access Google Sheets'); },
      getActiveSpreadsheet: () => { throw new Error('Thin intake must never access Google Sheets'); }
    },
    GmailApp: {
      getUserLabelByName: (n) => labels.get(n) || null,
      createLabel: (n) => makeLabel(n),
      search: (_q, _s, max) => threads.filter((t) => !t.isSpam && !t.isTrash && (t.isUnread() || !t._labels.has('PB-Support/Processed'))).slice(0, max),
      sendEmail: () => { sentEmails.push('FORBIDDEN_SEND'); throw new Error('Thin intake must never send email'); },
      createDraft: () => { drafts.push('FORBIDDEN_DRAFT'); throw new Error('Thin intake must never create drafts'); }
    },
    UrlFetchApp: {
      fetch: (url, params) => {
        fetchCalls.push({ url, params });
        const res = urlFetchHandler(url, params);
        return {
          getResponseCode: () => res.status ?? 200,
          getContentText: () => JSON.stringify(res.body)
        };
      }
    },
    ScriptApp: {
      getProjectTriggers: () => triggers.map((t) => ({ getHandlerFunction: () => t.handler })),
      deleteTrigger: () => { triggers.length = 0; },
      newTrigger: (handler) => ({
        timeBased: () => ({
          everyMinutes: (m) => ({ create: () => triggers.push({ handler, everyMinutes: m }) })
        })
      })
    }
  };

  vm.createContext(sandbox);
  for (const file of ['Config.gs', 'Safeguards.gs', 'Intake.gs', 'Main.gs']) {
    vm.runInContext(fs.readFileSync(path.join(APPS_SCRIPT_DIR, file), 'utf8'), sandbox, { filename: file });
  }
  return { ctx: sandbox, addThread, fetchCalls, sentEmails, drafts, triggers };
}

test('Thin intake forwards clear voicemail & email payloads to backend, redacts cards, ignores SMS/bounces, and NEVER sends email or touches Sheets', () => {
  const { ctx, addThread, fetchCalls, sentEmails, drafts } = createThinIntakeSandbox({
    urlFetchHandler: (_url, params) => {
      const body = JSON.parse(params.payload);
      if (body.support_code === 'PB-48213') {
        return { status: 200, body: { outcome: 'AUTO_REPLIED', support_code: 'PB-48213', intent: 'APPEAL_STATUS' } };
      }
      return { status: 200, body: { outcome: 'NO_ID_NO_RESPONSE', support_code: null, intent: 'NO_ID' } };
    }
  });

  // 1. Clear Google Voice voicemail with spoken PB-48213 + credit card number that must be redacted before leaving Workspace
  const vmThread = addThread({
    id: 't-vm-1',
    messages: [{
      id: 'gm-vm-1',
      from: 'Google Voice <voice-noreply@google.com>',
      subject: 'New voicemail from (415) 555-0142 at 10:00 AM',
      body: 'Transcript:\nMy case ID is P B dash 4 8 2 1 3 and card 4532 0151 1283 0366, where is my appeal?\n\nPlay message\nGoogle LLC'
    }]
  });

  // 2. Email with NO case ID -> still forwarded to backend with support_code: null so backend logs NO_ID_NO_RESPONSE
  const noIdThread = addThread({
    id: 't-noid-1',
    messages: [{
      id: 'gm-noid-1',
      from: 'driver@example.com',
      subject: 'Where is my ticket?',
      body: 'Checking on my ticket without any code.'
    }]
  });

  // 3. Google Voice SMS -> ignored locally (strict no-SMS policy)
  const smsThread = addThread({
    id: 't-sms-1',
    messages: [{
      id: 'gm-sms-1',
      from: 'Google Voice <voice-noreply@google.com>',
      subject: 'New text message from (415) 555-0142',
      body: 'PB-48213 status?'
    }]
  });

  const summary = ctx.pollSupportInbox();

  assert.equal(summary.forwarded, 2);
  assert.equal(summary.autoReplied, 1);
  assert.equal(summary.noIdNoResponse, 1);
  assert.equal(summary.ignored, 1);
  assert.equal(sentEmails.length, 0);
  assert.equal(drafts.length, 0);
  assert.equal(fetchCalls.length, 2);

  // Verify Bearer auth & redacted voicemail payload
  assert.equal(fetchCalls[0].params.headers.Authorization, 'Bearer test-intake-secret-0123456789');
  assert.equal(fetchCalls[0].params.followRedirects, false);
  const vmPayload = JSON.parse(fetchCalls[0].params.payload);
  assert.equal(vmPayload.channel, 'voicemail');
  assert.equal(vmPayload.support_code, 'PB-48213');
  assert.ok(vmPayload.text.includes('[REDACTED_CARD]'));
  assert.ok(!vmPayload.text.includes('4532'));
  assert.ok(!vmPayload.text.includes('Google LLC'));
  assert.ok(vmThread._labels.has('PB-Support/Auto-Replied'));

  // Verify no-ID payload
  const noIdPayload = JSON.parse(fetchCalls[1].params.payload);
  assert.equal(noIdPayload.channel, 'email');
  assert.equal(noIdPayload.support_code, null);
  assert.ok(noIdThread._labels.has('PB-Support/No-ID-No-Response'));
  assert.ok(smsThread._labels.has('PB-Support/Ignored'));

  // Verify manifest has NO spreadsheets or gmail.send scopes
  const manifest = JSON.parse(fs.readFileSync(path.join(APPS_SCRIPT_DIR, 'appsscript.json'), 'utf8'));
  assert.ok(!manifest.oauthScopes.some((s) => s.includes('spreadsheets')));
  assert.ok(!manifest.oauthScopes.some((s) => s.includes('gmail.send')));
});

test('intake refuses HTTP and requires its own credential', () => {
  for (const properties of [
    { PB_BACKEND_INTAKE_URL: 'http://api.parkingbreaker.example/internal/support/intake' },
    { PB_SUPPORT_INTAKE_SECRET: '', PB_WORKER_SECRET: 'old-worker-secret' }
  ]) {
    const { ctx, fetchCalls } = createThinIntakeSandbox({ scriptProperties: properties });
    assert.throws(() => ctx.postToBackendIntake({}, ctx.getIntakeConfig()), /HTTPS support intake/);
    assert.equal(fetchCalls.length, 0);
  }
});

test('a transport exception leaves the message unread and forwards later messages without retrying it in the same poll', () => {
  const { ctx, addThread, fetchCalls, sentEmails, drafts } = createThinIntakeSandbox({
    urlFetchHandler: (_url, params) => {
      if (JSON.parse(params.payload).message_id === 'gm-failed') {
        throw new Error('simulated transport failure');
      }
      return { body: { outcome: 'NO_ID_NO_RESPONSE', intent: 'NO_ID' } };
    }
  });
  const thread = addThread({
    id: 't-retry',
    messages: [
      { id: 'gm-failed', from: 'driver@example.com', body: 'First request', unread: false },
      { id: 'gm-later', from: 'driver@example.com', body: 'Second request' }
    ]
  });

  const summary = ctx.pollSupportInbox();

  assert.equal(summary.deliveryErrors, 1);
  assert.equal(summary.forwarded, 1);
  assert.deepEqual(fetchCalls.map(({ params }) => JSON.parse(params.payload).message_id), ['gm-failed', 'gm-later']);
  assert.ok(thread._labels.has('PB-Support/Intake-Retry'));
  assert.deepEqual(thread.getMessages().map((message) => message.isUnread()), [true, false]);
  assert.equal(sentEmails.length, 0);
  assert.equal(drafts.length, 0);
});

test('a 2xx response without a recognized intake disposition leaves the message unread and unprocessed', () => {
  const { ctx, addThread } = createThinIntakeSandbox({
    urlFetchHandler: () => ({ body: { outcome: 'UNKNOWN' } })
  });
  const thread = addThread({
    id: 't-malformed',
    messages: [{ id: 'gm-malformed', from: 'driver@example.com', body: 'Support request' }]
  });

  const summary = ctx.pollSupportInbox();

  assert.equal(summary.deliveryErrors, 1);
  assert.equal(summary.forwarded, 0);
  assert.deepEqual([...thread._labels], ['PB-Support/Intake-Retry']);
  assert.ok(thread.getMessages()[0].isUnread());
});
