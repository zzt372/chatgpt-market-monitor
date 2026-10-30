import { chromium } from "playwright";
import fs from "node:fs/promises";

const URLS = ["https://www.flexispot.jp/e7-pro.html", "https://www.flexispot.jp/new/e7-pro.html"];
const OUT = new URL("./latest.json", import.meta.url);
const DIAG = new URL("./diagnostic.json", import.meta.url);

function amount(field) {
  const n = Number(field && typeof field === "object" ? field.value : field);
  return Number.isInteger(n) && n >= 20000 && n <= 150000 ? n : null;
}
function skuPrices(data) {
  if (data?.id !== 170087 || !/E7 Pro/.test(data?.itemRenderTO?.itemName || "")) throw new Error("Wrong official product identifier");
  const group = (data.mainItemList || []).find(g => Array.isArray(g.skuExtList) && g.skuExtList.some(x => x.skuCode === "E7WPro"));
  if (!group) throw new Error("E7 Pro frame SKU group missing");
  return [["E7WPro","ホワイト"], ["E7BPro","ブラック"]].map(([code, color]) => {
    const matches = group.skuExtList.filter(x => x.skuCode === code);
    if (matches.length !== 1) throw new Error("Invalid SKU count: " + code);
    const sale = amount(matches[0].salePrc);
    const original = amount(matches[0].originalPrc);
    if (sale === null || original === null) throw new Error("Missing/invalid SKU price: " + code);
    return { color, sku_code: code, price_jpy: sale, regular_price_jpy: original };
  });
}
function parseVisiblePrice(s) {
  const match = String(s).match(/[￥¥]\s*([\d,]+)/);
  if (!match) return null;
  const n = Number(match[1].replaceAll(",", ""));
  return Number.isInteger(n) && n >= 20000 && n <= 150000 ? n : null;
}
async function checkPage(browser, url) {
  const context = await browser.newContext({ locale:"ja-JP", timezoneId:"Asia/Tokyo" });
  const page = await context.newPage();
  let apiDetail = null;
  let apiError = null;
  const result = { url, http_status:null, api_product_id:null, api_variants:null, visible_color:null, visible_top:null, visible_price:null, visible_buy_enabled:false };
  page.on("response", async response => {
    if (!response.url().includes("/sapi/mall-item/item/detail") || response.status() !== 200) return;
    try {
      const j = await response.json();
      if (j?.code !== 200 || j?.result !== true) throw new Error("Official item API unsuccessful");
      apiDetail = j.data;
    } catch (e) { apiError = String(e).slice(0,300); }
  });
  try {
    const initial = await page.goto(url, { waitUntil:"domcontentloaded", timeout:55000 });
    result.http_status = initial?.status() ?? null;
    await page.waitForLoadState("networkidle", { timeout:18000 }).catch(() => {});
    const buy = page.locator("button.add-cart.controller-button").first();
    await buy.waitFor({state:"visible",timeout:15000});
    await page.waitForTimeout(900);
    const body = await page.locator("body").innerText();
    result.visible_color = body.match(/カラー：\s*(ホワイト|ブラック)/)?.[1] || null;
    result.visible_top = body.match(/天板のタイプ：\s*(無し|長方形|カーブ型|集成材・無垢材|配線スリット付き|モニタースタンド付き)/)?.[1] || null;
    result.visible_price = parseVisiblePrice(await buy.innerText());
    result.visible_buy_enabled = await buy.isEnabled();
    result.model_visible = /電動昇降式デスク・\s*E7 Pro/.test(body);
    if (!apiDetail) {
      await page.waitForTimeout(1400);
    }
    if (!apiDetail) throw new Error("Official detail API response not captured: " + (apiError || "none"));
    result.api_product_id = apiDetail.id;
    result.api_variants = skuPrices(apiDetail);
    result.product_level_price_jpy = amount(apiDetail.shopSkuList?.[0]?.salePrc);
  } catch (e) {
    result.error = String(e).slice(0,450);
  } finally {
    await context.close();
  }
  return result;
}
function rowValid(row) {
  const white = row?.api_variants?.find(v => v.sku_code === "E7WPro");
  const black = row?.api_variants?.find(v => v.sku_code === "E7BPro");
  return row?.http_status === 200 && row.api_product_id === 170087 &&
    row.model_visible === true && row.visible_color === "ホワイト" && row.visible_top === "無し" &&
    row.visible_buy_enabled === true && row.visible_price === white?.price_jpy &&
    Number.isInteger(black?.price_jpy);
}
async function checkPageWithRetry(browser, url) {
  const attempts = [];
  let row = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      row = await checkPage(browser, url);
    } catch (e) {
      row = {url, error:String(e).slice(0,450)};
    }
    const passed = rowValid(row);
    attempts.push({attempt, passed, error:row.error || null});
    if (passed) return {row, attempts};
    console.warn(`E7 Pro page ${url} failed attempt ${attempt}/3: ${row.error || "DOM/SKU verification failed"}`);
    if (attempt < 3) await new Promise(resolve => setTimeout(resolve, 1500 * attempt));
  }
  return {row, attempts};
}
const browser = await chromium.launch({headless:true});
const checks=[];
const attempt_history=[];
try {
  for (const url of URLS) {
    const {row, attempts} = await checkPageWithRetry(browser, url);
    checks.push(row);
    attempt_history.push({url, attempts});
  }
} finally {
  await browser.close();
}
const checkedAt = new Date().toISOString();
const valid = checks.length === 2 && checks.every(rowValid) &&
  JSON.stringify(checks[0].api_variants) === JSON.stringify(checks[1].api_variants);
await fs.writeFile(DIAG,JSON.stringify({schema_version:1,checked_at:checkedAt,valid,checks,attempt_history},null,2)+"\n","utf8");
if (!valid) {
  console.error("Official E7 Pro SKU price verification failed",checks);
  process.exitCode=1;
} else {
  const variants=checks[0].api_variants;
  const min=Math.min(...variants.map(x=>x.price_jpy));
  const result={
    schema_version:1,ok:true,product_id:170087,product_name:"FlexiSpot E7 Pro",
    market:"JP",currency:"JPY",source_kind:"official-product-browser-rendered",
    checked_at:checkedAt,source_urls:URLS,frame_only:true,desktop_type:"無し",
    verified_variants:variants,all_colors_verified:true,lowest_verified_frame_price_jpy:min,
    checkout_and_coupon_verified:false,
    verification:"Two independent browser sessions on two official URLs; each captures official /sapi/mall-item/item/detail SKU prices for E7WPro and E7BPro, and confirms E7WPro against an enabled white frame-only purchase button.",
    notes:"Official per-color frame price, excluding tabletop. Coupon-stacked checkout amount not tested; do not deduct unverified coupons."
  };
  await fs.writeFile(OUT,JSON.stringify(result,null,2)+"\n","utf8");
  console.log(JSON.stringify(result,null,2));
}