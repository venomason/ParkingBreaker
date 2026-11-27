// Run with the image's configured user and no external network.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";

assert.notEqual(process.getuid(), 0, "The customer site must run as a non-root user");
const origin = "http://127.0.0.1:3117";
const server = spawn(process.execPath, [
  "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", "3117",
], { stdio: ["ignore", "inherit", "inherit"] });
const exited = once(server, "exit");
try {
  let privacy;
  for (let tries = 0; tries < 40; tries++) {
    assert.equal(server.exitCode, null, "The website exited during startup");
    try {
      privacy = await fetch(origin + "/privacy", { signal: AbortSignal.timeout(3000) });
      break;
    } catch {
      await delay(250);
    }
  }
  assert.equal(privacy?.status, 200, "The image must serve its privacy page");
  assert.match(await privacy.text(), /Privacy Policy/i);
  const brand = await fetch(origin + "/images/brand/parkingbreaker-pb-shell.webp", {
    signal: AbortSignal.timeout(3000),
  });
  assert.equal(brand.status, 200, "The image must serve its packaged assets");
  assert.equal(Buffer.from(await brand.arrayBuffer()).toString("ascii", 8, 12), "WEBP");
  console.log("Non-root website startup, privacy page and packaged asset passed");
} finally {
  if (server.exitCode === null && server.signalCode === null) server.kill("SIGTERM");
  await exited;
}
