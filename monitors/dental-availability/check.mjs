import { chromium } from "playwright";
import fs from "node:fs/promises";

const TARGET_URL = "https://reservation.stransa.co.jp/7aad7af9344abd6917921bf71b809583";
const MENU = "痛い、かぶせ物が取れた etc";
const TARGET_YEAR = 2026;
const TARGET_MONTH = 10;
const OUT = new URL("./latest.json", import.meta.url);
const DIAG = new URL("./diagnostic.json", import.meta.url);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function safeText(locator) {
  try { return (await locator.innerText()).trim(); } catch { return ""; }
}

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

async function clickMenu(page) {
  const candidates = [
    page.getByText(MENU, { exact: true }),
    page.getByText(MENU, { exact: false }),
    page.locator("label", { hasText: "痛い" }),
    page.locator("button", { hasText: "痛い" }),
    page.locator('[role="button"]', { hasText: "痛い" })
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

  // Radio/checkbox/select fallback based on nearby text.
  const label = page.locator("label", { hasText: "痛い" }).first();
  if (await label.count()) {
    try {
      await label.click();
      await sleep(1200);
      return true;
    } catch {}
  }
  return false;
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

async function extractTimesAfterClick(page, day) {
  const before = page.url();
  const dayTexts = [String(day), `${day}日`];
  const clickables = page.locator("button, [role=button], a");
  const n = await clickables.count();
  for (let i = 0; i < n; i++) {
    const el = clickables.nth(i);
    if (!(await el.isVisible().catch(() => false))) continue;
    if (await el.isDisabled().catch(() => false)) continue;
    const txt = (await safeText(el)).replace(/\s+/g, " ");
    const aria = await el.getAttribute("aria-label").catch(() => "");
    if (!dayTexts.includes(txt) && !dayTexts.some((x) => (aria || "").trim() === x)) continue;
    try {
      await el.click({ timeout: 2000 });
      await sleep(500);
      const texts = await page.locator("button, [role=button], a, option, label").allInnerTexts();
      const times = [...new Set(texts.flatMap((t) => {
        const ms = t.match(/(?:[01]?\d|2[0-3]):[0-5]\d/g);
        return ms || [];
      }))].sort();
      if (times.length) return times;
      if (page.url() !== before) await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => {});
    } catch {}
  }
  return [];
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({
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

  if (!(await clickMenu(page))) {
    await snapshot(page, "menu-not-found");
    throw new Error(`Menu not found: ${MENU}`);
  }

  await page.waitForLoadState("networkidle", { timeout: 12000 }).catch(() => {});
  await sleep(1000);
  await moveToTargetMonth(page);

  const dayControls = await collectDayControls(page);
  const available = dayControls.filter((x) => x.available).map((x) => x.day);

  // If DOM heuristics find nothing, keep diagnostics and fail instead of publishing a false empty state.
  if (dayControls.length < 7) {
    await snapshot(page, "calendar-controls-not-recognized");
    throw new Error(`Calendar recognition failed: only ${dayControls.length} day-like controls`);
  }

  const slots = {};
  for (const day of available) {
    slots[String(day)] = await extractTimesAfterClick(page, day);
    // Return to target view if date click changed state.
    if (!page.url().startsWith(TARGET_URL)) {
      await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
      await clickMenu(page);
      await moveToTargetMonth(page);
    }
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
    slots,
    granularity: Object.values(slots).some((v) => v.length) ? "time" : "date",
    notes: "Availability is extracted with Playwright from the live reservation UI. Empty slot arrays mean day-level availability was detectable but time buttons were not exposed in the current UI step."
  };
  await fs.writeFile(OUT, JSON.stringify(result, null, 2) + "\n", "utf8");
  await snapshot(page, "success");
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
