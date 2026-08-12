// atomic_reader.mjs — headless Playwright reader for Atomic Mail.
// Logs in (session cached per-email), polls inbox for a verification code,
// prints the 6-digit code to stdout. Called by Python atomic_client.
//
// Env vars:
//   ATOMIC_EMAIL     full email, e.g. yourname@atomicmail.io
//   ATOMIC_PASSWORD  account password
//   SENDER_HINT      substring to match sender (default "wpn")
//   SUBJECT_HINT     substring to match subject (default "")
//   TIMEOUT_SEC      max wait (default 90)
//   POLL_SEC         poll interval (default 5)
//   PROFILE_DIR      base dir for persistent profiles (default ./.atomic_profiles)
//   MODE             "read" (default) = wait for a NEW code; "list" = print current
//                    matching message hrefs as JSON and exit
//   EXCLUDE_HREFS    comma-separated hrefs to ignore (stale emails). In "read" mode
//                    the reader returns the newest NON-excluded matching code.
//
// Exit 0 + code (read) / JSON hrefs (list) on stdout. Exit 1 on failure.

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

const EMAIL = process.env.ATOMIC_EMAIL;
const PASSWORD = process.env.ATOMIC_PASSWORD;
const SENDER_HINT = (process.env.SENDER_HINT || "wpn").toLowerCase();
const SUBJECT_HINT = (process.env.SUBJECT_HINT || "").toLowerCase();
const TIMEOUT_SEC = parseInt(process.env.TIMEOUT_SEC || "90", 10);
const POLL_SEC = parseInt(process.env.POLL_SEC || "5", 10);
const PROFILE_DIR = process.env.PROFILE_DIR || "./.atomic_profiles";
const MODE = process.env.MODE || "read";
const EXCLUDE_HREFS = (process.env.EXCLUDE_HREFS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

if (!EMAIL || !PASSWORD) {
  console.error("ATOMIC_EMAIL and ATOMIC_PASSWORD are required");
  process.exit(2);
}

const CODE_RE = /\b(\d{6})\b/;
const localPart = EMAIL.split("@")[0];
const profilePath = join(PROFILE_DIR, localPart);
mkdirSync(profilePath, { recursive: true });

const SIGNIN = "https://atomicmail.io/app/auth/sign-in";
const APP = "https://atomicmail.io/app";

async function ensureLoggedIn(page) {
  // If already authenticated, /app redirects to the mailbox.
  await page.goto(APP, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  const url = page.url();
  if (url.includes("/auth/sign-in")) {
    await login(page);
  }
}

async function login(page) {
  await page.goto(SIGNIN, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);

  // step 1: email local part
  const emailInput = page.getByRole("textbox", { name: "e.g. alfie.hitchcock" });
  await emailInput.fill(localPart);
  await page.getByRole("button", { name: "Submit" }).click();
  await page.waitForTimeout(1500);

  // step 2: password
  const pwd = page.getByRole("textbox", { name: "e.g. 1Jsh3!ajK" });
  await pwd.fill(PASSWORD);
  await page.getByRole("button", { name: "Sign In" }).click();

  // wait for redirect away from sign-in
  await page.waitForURL((u) => !u.toString().includes("/auth/sign-in"), {
    timeout: 30000,
  });
  await page.waitForTimeout(1500);
}

// Scan inbox list; return array of {href, sender, subject, preview, ageText}.
function scanInbox() {
  const out = [];
  const links = Array.from(document.querySelectorAll('a[href*="/message/"]'));
  for (const a of links) {
    const txt = (a.innerText || "").replace(/\s+/g, " ").trim();
    out.push({ href: a.getAttribute("href"), text: txt });
  }
  return out;
}

// Read opened message body text from main region.
function readMessageBody() {
  const main = document.querySelector("main");
  return main ? main.innerText : document.body.innerText;
}

async function findCode(page, context) {
  // Land on the inbox root and force a fresh load.
  await page.goto(APP, { waitUntil: "domcontentloaded" });
  // SPA fetches the message list from JMAP — wait until links appear.
  let loaded = false;
  for (let i = 0; i < 20; i++) {
    const n = await page.evaluate(
      () => document.querySelectorAll('a[href*="/message/"]').length
    );
    if (n > 0) { loaded = true; break; }
    await page.waitForTimeout(500);
  }
  if (!loaded) {
    process.stderr.write("[atomic_reader] inbox list did not render\n");
    return null;
  }

  const links = await page.evaluate(scanInbox);
  // match by sender/subject hint in the link text
  let candidates = links.filter((l) => {
    const t = (l.text || "").toLowerCase();
    return (
      (SENDER_HINT === "" || t.includes(SENDER_HINT)) &&
      (SUBJECT_HINT === "" || t.includes(SUBJECT_HINT))
    );
  });
  // drop stale emails the caller already knows about
  if (EXCLUDE_HREFS.length) {
    candidates = candidates.filter((c) => !EXCLUDE_HREFS.includes(c.href));
  }

  if (!candidates.length) return null;

  // 1. WPN puts the code right in the preview text — try that first (fast).
  for (const c of candidates) {
    const m = CODE_RE.exec(c.text || "");
    if (m) return m[1];
  }

  // 2. Fallback: open each matching message and read the body.
  for (const c of candidates) {
    await page.goto("https://atomicmail.io" + c.href, {
      waitUntil: "domcontentloaded",
    });
    await page.waitForTimeout(1500);
    const body = await page.evaluate(readMessageBody);
    const m = CODE_RE.exec(body);
    if (m) return m[1];
  }
  return null;
}

// list mode: print current matching hrefs as JSON (baseline snapshot)
async function listHrefs(page) {
  await page.goto(APP, { waitUntil: "domcontentloaded" });
  for (let i = 0; i < 20; i++) {
    const n = await page.evaluate(
      () => document.querySelectorAll('a[href*="/message/"]').length
    );
    if (n > 0) break;
    await page.waitForTimeout(500);
  }
  const links = await page.evaluate(scanInbox);
  const hrefs = links
    .filter((l) => {
      const t = (l.text || "").toLowerCase();
      return (
        (SENDER_HINT === "" || t.includes(SENDER_HINT)) &&
        (SUBJECT_HINT === "" || t.includes(SUBJECT_HINT))
      );
    })
    .map((l) => l.href);
  return hrefs;
}

(async () => {
  const context = await chromium.launchPersistentContext(profilePath, {
    headless: true,
    viewport: { width: 1280, height: 900 },
  });
  const page = context.pages()[0] || (await context.newPage());

  try {
    await ensureLoggedIn(page);

    // list mode: snapshot current matching hrefs and exit
    if (MODE === "list") {
      const hrefs = await listHrefs(page);
      process.stdout.write(JSON.stringify(hrefs));
      await context.close();
      process.exit(0);
    }

    const deadline = Date.now() + TIMEOUT_SEC * 1000;
    let attempt = 0;
    while (Date.now() < deadline) {
      attempt++;
      process.stderr.write(`[atomic_reader] attempt ${attempt}\n`);
      try {
        const code = await findCode(page, context);
        if (code) {
          process.stdout.write(code);
          await context.close();
          process.exit(0);
        }
      } catch (e) {
        process.stderr.write(`[atomic_reader] scan error: ${e.message}\n`);
      }
      await page.waitForTimeout(POLL_SEC * 1000);
    }
    process.stderr.write(
      `[atomic_reader] no code within ${TIMEOUT_SEC}s for ${EMAIL}\n`
    );
    await context.close();
    process.exit(1);
  } catch (e) {
    process.stderr.write(`[atomic_reader] fatal: ${e.message}\n`);
    try {
      await context.close();
    } catch {}
    process.exit(1);
  }
})();
