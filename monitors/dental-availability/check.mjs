import { chromium } from "playwright";
import fs from "node:fs/promises";

const TARGET_URL = "https://reservation.stransa.co.jp/7aad7af9344abd6917921bf71b809583";
const MENU = "痛い、かぶせ物が取れた etc";
const TARGET_YEAR = 2026;
const TARGET_MONTH = 10;
const OUT = new URL("./latest.json", import.meta.url);
const DIAG = new URL("./diagnostic.json", import.meta.url);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function snapshot(page, note) {
  const bodyText = (await page.locator("body").innerText().catch(() => "")).slice(0, 12000);
  const nodes = await page.locator("button, [role=button], a, input, label, select, option").evaluateAll((els) =>
    els.slice(0, 400).map((el) => ({
      tag: el.tagName,
      text: (el.innerText || el.textContent || "").trim().slice(0, 300),
      aria: el.getAttribute("aria-label"),
      title: el.getAttribute("title"),
      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
      value: el.value ?? null,
      type: el.getAttribute("type"),
      name: el.getAttribute("name"),
      cls: String(el.className || "").slice(0, 300)
    }))
  ).catch(() => []);
  const data = {
    checked_at: new Date().toISOString(),
    note,
    url: page.url(),
    title: await page.title().catch(() => ""),
    body_text: bodyText,
    nodes
  };
  await fs.writeFile(DIAG, JSON.stringify(data, null, 2) + "\n", "utf8");
  return data;
}

async function clickMenu(startPage) {
  const attemptCurrentPage = async (p) => {
    const candidates = [
      p.getByText(MENU, { exact: true }),
      p.getByText(MENU, { exact: false }),
      p.locator("label", { hasText: "痛い" }),
      p.locator("button", { hasText: "痛い" }),
      p.locator('[role="button"]', { hasText: "痛い" })
    ];
    for (const loc of candidates) {
      const count = await loc.count().catch(() => 0);
      for (let i = 0; i < count; i++) {
        const item = loc.nth(i);
        if (!(await item.isVisible().catch(() => false))) continue;
        try {
          await item.click({ timeout: 4000 });
          await sleep(1200);
          return true;
        } catch {}
      }
    }
    return false;
  };

  if (await attemptCurrentPage(startPage)) return startPage;

  for (const branch of ["初診予約", "再診予約"]) {
    if (startPage.url() !== TARGET_URL) {
      await startPage.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
      await startPage.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      await sleep(700);
    }

    const branchButton = startPage.getByRole("button", { name: branch, exact: true }).first();
    if (!(await branchButton.isVisible().catch(() => false))) continue;

    let branchPage = startPage;
    try {
      const popupPromise = startPage.waitForEvent("popup", { timeout: 2500 }).catch(() => null);
      await branchButton.click({ timeout: 4000 });
      const popup = await popupPromise;
      if (popup) {
        branchPage = popup;
        await branchPage.waitForLoadState("domcontentloaded", { timeout: 10000 }).catch(() => {});
      }
      await branchPage.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      await sleep(1000);

      if (await attemptCurrentPage(branchPage)) return branchPage;
      if (branchPage !== startPage) await branchPage.close().catch(() => {});
    } catch {
      if (branchPage !== startPage) await branchPage.close().catch(() => {});
    }
  }
  return null;
}

async function moveToTargetMonth(page) {
  const targetTokens = ["2026年10月", "2026 / 10", "2026/10", "2026-10", "10月 2026"];
  const bodyHasTarget = async () => {
    const t = await page.locator("body").innerText().catch(() => "");
    return targetTokens.some((x) => t.includes(x));
  };
  if (await bodyHasTarget()) return true;

  // Try next-month controls a limited number of times.
  for (let step = 0; step < 18; step++) {
    const nextCandidates = [
      page.getByRole("button", { name: /次|next|翌月|>/i }),
      page.locator('button[aria-label*="次"]'),
      page.locator('button[aria-label*="next" i]'),
      page.locator('[role="button"][aria-label*="次"]')
    ];
    let clicked = false;
    for (const c of nextCandidates) {
      const n = await c.count().catch(() => 0);
      for (let i = 0; i < n; i++) {
        const el = c.nth(i);
        if (!(await el.isVisible().catch(() => false))) continue;
        if (await el.isDisabled().catch(() => false)) continue;
        try {
          await el.click({ timeout: 2000 });
          await sleep(400);
          clicked = true;
          break;
        } catch {}
      }
      if (clicked) break;
    }
    if (!clicked) break;
    if (await bodyHasTarget()) return true;
  }
  return await bodyHasTarget();
}

function parseDay(text) {
  const s = String(text || "").trim();
  const m = s.match(/^(?:[日月火水木金土]\s*)?(\d{1,2})(?:\s*[日]?|\s*[○〇◯×✕✖])?$/);
  if (!m) return null;
  const d = Number(m[1]);
  return d >= 1 && d <= 31 ? d : null;
}

async function collectDayControls(page) {
  const raw = await page.locator("button, [role=button], td, a").evaluateAll((els) =>
    els.map((el, index) => {
      const txt = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return {
        index,
        tag: el.tagName,
        text: txt,
        aria: el.getAttribute("aria-label") || "",
        title: el.getAttribute("title") || "",
        disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
        visible: rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none",
        className: String(el.className || "")
      };
    })
  );

  const candidates = [];
  for (const x of raw) {
    if (!x.visible) continue;
    const day = parseDay(x.text) ?? parseDay(x.aria) ?? parseDay(x.title);
    if (!day) continue;
    const hint = (x.text + " " + x.aria + " " + x.title + " " + x.className).toLowerCase();
    const unavailableHint = /(disabled|unavailable|不可|満|×|✕|✖|closed|past)/.test(hint);
    const availableHint = /(available|予約可|空き|○|〇|◯|open|active|selectable)/.test(hint);
    candidates.push({ ...x, day, available: !x.disabled && !unavailableHint, availableHint });
  }

  // Deduplicate same day. Prefer explicit available/non-disabled button-ish controls.
  const byDay = new Map();
  for (const c of candidates) {
    const prev = byDay.get(c.day);
    const score = (c.available ? 10 : 0) + (c.availableHint ? 3 : 0) + (c.tag === "BUTTON" ? 2 : 0) + (c.tag === "A" ? 1 : 0);
    if (!prev || score > prev.score) byDay.set(c.day, { ...c, score });
  }
  return [...byDay.values()].sort((a,b) => a.day-b.day);
}

const browser = await chromium.launch({ headless: true });
let page = await browser.newPage({
  locale: "ja-JP",
  timezoneId: "Asia/Tokyo",
  viewport: { width: 1280, height: 1600 }
});

try {
  await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  await sleep(1200);

  // Close common overlays if present.
  for (const re of [/同意/, /閉じる/, /OK/i, /確認/]) {
    const b = page.getByRole("button", { name: re }).first();
    if (await b.isVisible().catch(() => false)) {
      await b.click().catch(() => {});
      await sleep(300);
    }
  }

  const menuPage = await clickMenu(page);
  if (!menuPage) {
    await snapshot(page, "menu-not-found");
    throw new Error(`Menu not found: ${MENU}`);
  }
  page = menuPage;

  await page.waitForLoadState("networkidle", { timeout: 12000 }).catch(() => {});
  await sleep(800);

  const otherDates = page.getByRole("button", { name: "他の日時を探す", exact: true }).first();
  if (await otherDates.isVisible().catch(() => false)) {
    await otherDates.click({ timeout: 4000 });
    await page.waitForLoadState("networkidle", { timeout: 12000 }).catch(() => {});
    await sleep(1000);
  }

  await moveToTargetMonth(page);

  const dayControls = await collectDayControls(page);
  const available = dayControls.filter((x) => x.available).map((x) => x.day);

  // If DOM heuristics find nothing, keep diagnostics and fail instead of publishing a false empty state.
  if (dayControls.length < 7) {
    await snapshot(page, "calendar-controls-not-recognized");
    throw new Error(`Calendar recognition failed: only ${dayControls.length} day-like controls`);
  }

  const result = {
    schema_version: 1,
    ok: true,
    checked_at: new Date().toISOString(),
    source_url: TARGET_URL,
    menu: MENU,
    year: TARGET_YEAR,
    month: TARGET_MONTH,
    available_dates: available.map((d) => `${TARGET_YEAR}-${String(TARGET_MONTH).padStart(2,"0")}-${String(d).padStart(2,"0")}`),
    granularity: "date",
    notes: "Availability is extracted with Playwright from the live reservation calendar. Monitoring intentionally uses date-level availability only."
  };
  await fs.writeFile(OUT, JSON.stringify(result, null, 2) + "\n", "utf8");
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
