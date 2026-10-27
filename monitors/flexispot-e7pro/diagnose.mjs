import { chromium } from "playwright";
import fs from "node:fs/promises";

const URLS = [
  "https://www.flexispot.jp/e7-pro.html",
  "https://www.flexispot.jp/new/e7-pro.html",
  "https://www.flexispot.jp/"
];
const OUT = new URL("./diagnostic.json", import.meta.url);
const now = new Date().toISOString();
const report = { schema_version: 1, checked_at: now, mode: "diagnostic", pages: [] };
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ locale: "ja-JP", timezoneId: "Asia/Tokyo", viewport: { width: 1365, height: 1100 } });
for (const url of URLS) {
  const page = await context.newPage();
  const row = { url, http_status: null, errors: [], network: [], body_text: "", price_texts: [], controls: [] };
  const interesting = /api|product|commodity|price|cart|sale|goods|detail/i;
  page.on("response", async (r) => {
    if (row.network.length >= 55) return;
    const u = r.url();
    if (!interesting.test(u) && !/json/i.test(r.headers()["content-type"] || "")) return;
    const item = { status: r.status(), url: u.slice(0, 300), content_type: (r.headers()["content-type"] || "").slice(0, 70) };
    if (/json/i.test(item.content_type) && /api|product|commodity|price|goods/i.test(u) && row.network.filter(x => x.sample).length < 5) {
      try { const t = await r.text(); item.sample = t.slice(0, 1800); } catch {}
    }
    row.network.push(item);
  });
  page.on("pageerror", (e) => { if(row.errors.length < 12) row.errors.push(String(e).slice(0, 300)); });
  try {
    const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    row.http_status = resp?.status() || null;
    await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(3000);
    row.title = await page.title();
    row.body_text = (await page.locator("body").innerText().catch(() => "")).slice(0, 11000);
    row.price_texts = row.body_text.match(/(?:￥|¥)\\s*(?:[0-9,]+|NaN)/g)?.slice(0, 40) || [];
    row.controls = await page.locator("button, input, select, option, label, [role=radio], [role=button]").evaluateAll((els) =>
      els.slice(0, 260).map((el) => ({
        tag: el.tagName,
        text: (el.innerText || el.textContent || "").trim().slice(0, 90),
        aria: el.getAttribute("aria-label"),
        title: el.getAttribute("title"),
        name: el.getAttribute("name"),
        value: (el.value || "").slice(0, 80),
        cls: String(el.className || "").slice(0, 90)
      })).filter(x => x.text || x.aria || x.name || x.value)
    ).catch(() => []);
    row.html_markers = await page.evaluate(() => {
      const html = document.documentElement.outerHTML;
      const needles = ["170087", "E7B-PRO", "E7W-PRO", "salePrc", "productId"];
      const o = {};
      for (const n of needles) {
        const i = html.indexOf(n);
        if (i >= 0) o[n] = html.slice(Math.max(0, i-150), i+300);
      }
      return o;
    }).catch(() => ({}));
  } catch (e) { row.errors.push(String(e).slice(0, 400)); }
  report.pages.push(row);
  await page.close().catch(() => {});
}
await fs.writeFile(OUT, JSON.stringify(report, null, 2)+"\\n");
console.log("E7 Pro diagnostic captured", report.pages.map(p => ({url:p.url,status:p.http_status,prices:p.price_texts,network:p.network.length,errors:p.errors.length})));
await browser.close();
