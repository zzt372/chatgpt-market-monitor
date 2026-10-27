import { chromium } from "playwright";
import fs from "node:fs/promises";

const URLS = [
  "https://www.flexispot.jp/e7-pro.html",
  "https://www.flexispot.jp/new/e7-pro.html"
];
const HOME = "https://www.flexispot.jp/";
const OUT = new URL("./latest.json", import.meta.url);
const DIAG = new URL("./diagnostic.json", import.meta.url);
const AMOUNT = /(?:￥|¥)\s*([0-9][0-9,]*)/;
const now = new Date().toISOString();
const checks = [];

function yen(text) {
  const m = String(text ?? "").match(AMOUNT);
  if (!m) return null;
  const n = Number(m[1].replaceAll(",", ""));
  return Number.isInteger(n) && n >= 20000 && n <= 150000 ? n : null;
}
function selection(text, label) {
  const re = label === "color" ? /カラー：\s*(ホワイト|ブラック)/ : /天板のタイプ：\s*(無し|長方形|カーブ型|集成材・無垢材|配線スリット付き|モニタースタンド付き)/;
  return String(text).match(re)?.[1] ?? null;
}
async function snapshot(page, url) {
  const body = await page.locator("body").innerText();
  const button = page.locator("button.add-cart.controller-button").first();
  const buttonText = await button.innerText({ timeout: 12000 });
  return {
    url,
    model_confirmed: /電動昇降式デスク・\s*E7 Pro/.test(body),
    color: selection(body, "color"),
    desktop_type: selection(body, "top"),
    button_text: buttonText.slice(0, 180),
    price: yen(buttonText),
    button_enabled: await button.isEnabled(),
    visible: await button.isVisible()
  };
}
async function pageCheck(browser, url) {
  const context = await browser.newContext({ locale: "ja-JP", timezoneId: "Asia/Tokyo" });
  const page = await context.newPage();
  let apiProductId = null;
  page.on("response", async (response) => {
    if (!response.url().includes("/sapi/mall-item/item/detail") || response.status() !== 200) return;
    try {
      const value = await response.json();
      if (value?.code === 200) apiProductId = value?.data?.id ?? null;
    } catch {}
  });
  const result = { url, checks: [] };
  try {
    const r = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 55000 });
    result.http_status = r?.status() ?? null;
    await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
    await page.locator("button.add-cart.controller-button").first().waitFor({ state: "visible", timeout: 15000 });
    await page.waitForTimeout(1000);
    const primary = await snapshot(page, url);
    result.checks.push(primary);
    result.api_product_id = apiProductId;
    result.color_selector_debug = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll("div, span, label")].filter(el => {
        const t = (el.textContent || "").trim();
        return (t === "カラー：" || t === "カラー：" + "ホワイト" || t === "カラー：" + "ブラック" || t === "ホワイト" || t === "ブラック") && el.children.length < 8;
      });
      return nodes.slice(0, 16).map(el => ({ text: (el.textContent || "").trim().slice(0, 80), self: el.outerHTML.slice(0, 700), parent: el.parentElement?.outerHTML.slice(0, 3000) }));
    }).catch(() => []);
    // Try a black frame only when a color selector explicitly identifies that option.
    const candidates = page.locator(
      'img[alt*="ブラック"], [title*="ブラック"], [aria-label*="ブラック"], [data-color="black"]'
    );
    const count = Math.min(await candidates.count(), 15);
    for (let i = 0; i < count; i++) {
      const el = candidates.nth(i);
      if (!(await el.isVisible().catch(() => false))) continue;
      try {
        await el.click({ timeout: 2500 });
        await page.waitForTimeout(1200);
        const selected = await snapshot(page, url);
        if (selected.color === "ブラック" && selected.desktop_type === "無し") {
          result.checks.push(selected);
          break;
        }
      } catch {}
    }
  } catch (err) {
    result.error = String(err).slice(0, 600);
  } finally {
    await context.close();
  }
  return result;
}
const browser = await chromium.launch({ headless: true });
try {
  for (const u of URLS) checks.push(await pageCheck(browser, u));
} finally {
  await browser.close();
}
const accepted = [];
for (const row of checks) {
  for (const c of row.checks) {
    if (row.http_status !== 200 || (row.api_product_id != null && row.api_product_id !== 170087)) continue;
    if (!c.model_confirmed || c.desktop_type !== "無し" || !c.visible || !c.button_enabled || !c.price) continue;
    if (!["ホワイト", "ブラック"].includes(c.color)) continue;
    accepted.push(c);
  }
}
const white = accepted.filter(x => x.color === "ホワイト");
const whiteVerified = white.length === 2 && white.every(x => x.price === white[0].price);
const black = accepted.filter(x => x.color === "ブラック");
const blackVerified = black.length === 2 && black.every(x => x.price === black[0].price);
let homepagePrice = null;
let homepageError = null;
try {
  const pageBrowser = await chromium.launch({ headless: true });
  const p = await pageBrowser.newPage({ locale: "ja-JP", timezoneId: "Asia/Tokyo" });
  await p.goto(HOME, { waitUntil: "domcontentloaded", timeout: 35000 });
  await p.waitForLoadState("networkidle", { timeout: 9000 }).catch(() => {});
  const body = await p.locator("body").innerText();
  const m = body.match(/E7 Pro（コの字型）\s*電動昇降式デスク・\s*E7 Pro\s*(￥[\d,]+)/);
  homepagePrice = m ? yen(m[1]) : null;
  await pageBrowser.close();
} catch (e) { homepageError = String(e).slice(0, 300); }
const diagnostic = { schema_version: 1, checked_at: now, checks, white_verified: whiteVerified, black_verified: blackVerified, homepage_price: homepagePrice, homepage_error: homepageError };
await fs.writeFile(DIAG, JSON.stringify(diagnostic, null, 2) + "\n", "utf8");
if (!whiteVerified) {
  console.error("E7 Pro frame-only WHITE price not independently confirmed on both official product URLs.");
  process.exitCode = 1;
} else {
  const variants = [{ color: "ホワイト", price_jpy: white[0].price }];
  if (blackVerified) variants.push({ color: "ブラック", price_jpy: black[0].price });
  const price = Math.min(...variants.map(x => x.price_jpy));
  const result = {
    schema_version: 1,
    ok: true,
    product_id: 170087,
    product_name: "FlexiSpot E7 Pro",
    market: "JP",
    currency: "JPY",
    source_kind: "official-product-browser-rendered",
    checked_at: now,
    source_urls: URLS,
    frame_only: true,
    desktop_type: "無し",
    verified_variants: variants,
    all_colors_verified: blackVerified,
    lowest_verified_frame_price_jpy: price,
    homepage_listing_price_jpy: homepagePrice,
    checkout_and_coupon_verified: false,
    notes: "Live Chromium DOM: each verified color price must agree across two independent official product URLs. Coupon application and final checkout require separate verification; do not claim discounted checkout amount."
  };
  await fs.writeFile(OUT, JSON.stringify(result, null, 2) + "\n", "utf8");
  console.log(JSON.stringify(result, null, 2));
}