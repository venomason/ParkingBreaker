// Run one synthetic intake through a previously authorized, test-mode HTTPS deployment.
// Usage: node tools/qualification/saugus-browser.cjs ORIGIN SHA FIXTURE_DIR NEW_RECEIPT [PORT CERT_PEM]
// Create FIXTURE_DIR with: backend/.venv/bin/python tools/qualification/saugus_ticket.py FIXTURE_DIR
const assert = require('node:assert/strict');
const { createHash, X509Certificate } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../../web/node_modules/playwright');
const hash = value => createHash('sha256').update(value).digest('hex');
async function checkedResponse(page, suffix, action) {
    const origin = new URL(page.url()).origin;
    const [response] = await Promise.all([
        page.waitForResponse(r => {
            const url = new URL(r.url());
            return url.origin === origin && url.pathname.endsWith(suffix) && r.request().method() === 'POST';
        }, { timeout: 120000 }),
        action(),
    ]);
    assert.equal(response.status(), 200, suffix);
    return response.json();
}
async function run() {
    const [origin, revision, fixtureDir, output, port, certificate] = process.argv.slice(2);
    assert(origin && fixtureDir && output, 'Supply origin, revision, fixture directory and a new receipt path.');
    const site = new URL(origin);
    assert(site.protocol === 'https:' && site.origin === origin && /^[a-f0-9]{40}$/.test(revision));
    const fixture = JSON.parse(fs.readFileSync(path.join(fixtureDir, 'manifest.json')));
    assert(fixture.fictional === true && /^PB-SIM-[A-F0-9]+$/.test(fixture.ticket));
    const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
    assert.equal(fixture.issue_date, day, 'Generate a fictional ticket dated for this run.');
    for (const side of ['front', 'back']) {
        assert.equal(hash(fs.readFileSync(path.join(fixtureDir, side + '.png'))), fixture.photo_sha256[side]);
    }
    const args = ['--no-proxy-server'];
    if (port || certificate) {
        assert(certificate && /^\d+$/.test(port) && Number(port) > 0 && Number(port) <= 65535);
        const cert = new X509Certificate(fs.readFileSync(certificate));
        assert.equal(cert.checkHost(site.hostname), site.hostname);
        const pin = createHash('sha256').update(cert.publicKey.export({ type: 'spki', format: 'der' })).digest('base64');
        args.push('--host-resolver-rules=MAP ' + site.hostname + ' 127.0.0.1:' + port, '--ignore-certificate-errors-spki-list=' + pin);
    }
    const receipt = { started_utc: new Date().toISOString(), origin, revision, photo_sha256: fixture.photo_sha256, events: [] };
    const fd = fs.openSync(output, 'wx', 0o600);
    fs.closeSync(fd);
    const record = (event, details = {}) => {
        receipt.events.push({ utc: new Date().toISOString(), event, ...details });
        fs.writeFileSync(output + '.tmp', JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
        fs.renameSync(output + '.tmp', output);
    };
    let browser;
    try {
        browser = await chromium.launch({ channel: 'chrome', headless: true, args });
        const page = await browser.newPage({ serviceWorkers: 'block' });
        await page.goto(origin);
        assert.equal(new URL(page.url()).origin, origin);
        const health = await page.evaluate(async () => (await fetch('/api/health')).json());
        record('observed-health', { mode: health.mode, build_commit: health.build_commit });
        assert.equal(health.mode, 'test');
        assert.equal(health.build_commit, revision);
        assert.equal(await page.evaluate(async () => (await fetch('/api/auth/session')).status), 401);
        record('verified-test-build');
        await page.getByRole('button', { name: 'Start an appeal', exact: true }).click();
        await page.getByRole('button', { name: 'Close private access', exact: true }).waitFor();
        await page.waitForLoadState('networkidle');
        await page.getByRole('button', { name: 'Add ticket photo →' }).click();
        for (const side of ['front', 'back']) {
            await page.getByRole('button', { name: 'Choose ' + side + ' photo' }).setInputFiles(path.join(fixtureDir, side + '.png'));
        }
        const extract = await checkedResponse(page, '/ticket-reading', () => page.locator('#visionReadBtn').click());
        record('extraction', { status: extract.status, model: extract.primary_model, synthetic: extract.synthetic, fields_sha256: hash(JSON.stringify(extract.fields)) });
        assert(extract.status === 'success' && extract.synthetic === false);
        await page.locator('#visionSuggestions').waitFor();
        const values = { citation_number: fixture.ticket, license_plate: 'SIM273', license_plate_state: 'MA', issue_date: fixture.issue_date, issuer_hint: 'TOWN OF SAUGUS, MA', issued_city: 'SAUGUS', issued_state: 'MA', violation_code: 'No violation code printed', violation_description: 'Expired meter', stage: 'Initial appeal' };
        for (const [field, value] of Object.entries(values)) {
            const input = page.locator('#fact-' + field);
            if (await input.inputValue() !== value)
                await input.fill(value);
        }
        await page.locator('#fact-issuer_kind-government').check();
        for (const field of ['notice_date', 'printed_deadline'])
            await page.locator('#fact-' + field + '-absent').check();
        const review = await checkedResponse(page, '/ticket-review', () => page.locator('#visionReviewBtn').click());
        record('comparison', { status: review.status, synthetic: review.synthetic, fields_sha256: hash(JSON.stringify(review.fields)) });
        assert(review.status === 'success' && review.synthetic === false);
        await page.locator('#visionReview').waitFor();
        for (const field of await page.locator('select[id^=resolve-]').all()) {
            const id = await field.getAttribute('id');
            const choices = await field.locator('option').evaluateAll(ns => ns.map(n => n.value).filter(Boolean));
            assert(['resolve-issued_city', 'resolve-issued_state'].includes(id) && choices.length === 1 && choices[0] === 'ABSENCE_ACKNOWLEDGED', 'Unexpected discrepancy requires fixture review.');
            await field.selectOption(choices[0]);
        }
        await page.locator('#visionAccuracyAcknowledged').check();
        await page.locator('#visionConfirmBtn').click();
        await page.locator('#visionConfirmed').waitFor();
        record('customer-confirmed');
        await page.locator('#actContinueTicket').click();
        await page.locator('#story').fill('The meter display showed time remaining when I parked.');
        await page.locator('#actSendStory').click();
        await page.getByRole('button', { name: /^Yes/ }).first().click();
        const contact = { liveName: 'Jordan Sample', livePhone: '619-555-0114', liveEmail: 'filing-sim@example.test', liveLine1: '10 Fiction Way', liveCity: 'San Diego', liveState: 'CA', liveZip: '92101' };
        for (const [field, value] of Object.entries(contact))
            await page.locator('#' + field).fill(value);
        await page.locator('#liveRentalPaid').selectOption('not-rental');
        const decisionResponse = page.waitForResponse(r => {
            const url = new URL(r.url());
            return url.origin === origin && url.pathname.endsWith('/pre-hold') && r.request().method() === 'GET';
        }, { timeout: 120000 });
        const [intake, response] = await Promise.all([
            checkedResponse(page, '/intake', () => page.locator('#liveSubmit').click()), decisionResponse,
        ]);
        assert.equal(response.status(), 200);
        const bytes = await response.body();
        const decision = JSON.parse(bytes.toString());
        assert.equal(decision.case_id, intake.case_id);
        const status = await page.evaluate(async (id) => (await fetch('/api/cases/' + encodeURIComponent(id) + '/status')).json(), intake.case_id);
        record('filing-decision', { http: response.status(), case_id_sha256: hash(intake.case_id), response_sha256: hash(bytes), state: status.state, decision: { ...decision, case_id: '[redacted]' } });
        return decision.kind;
    }
    catch (error) {
        record('incomplete', { last_step: receipt.events.at(-1)?.event ?? 'startup', error_type: error.name });
        throw error;
    }
    finally {
        if (browser)
            await browser.close();
    }
}
run().then(kind => console.log('Recorded filing decision:', kind)).catch(error => { console.error(error.message); process.exitCode = 1; });
