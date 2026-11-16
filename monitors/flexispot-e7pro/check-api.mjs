import fs from "node:fs/promises";

const API = "https://www.flexispot.jp/sapi/mall-item/item/detail";
const PRODUCT_URLS = ["https://www.flexispot.jp/e7-pro.html","https://www.flexispot.jp/new/e7-pro.html"];
const BODY = JSON.stringify({ urlKey: "e7-pro.html", origin: "JP" });
const OUT = new URL("./latest.json", import.meta.url);
const DIAG = new URL("./diagnostic.json", import.meta.url);
const checkedAt = new Date().toISOString();

function price(v) {
  const n = Number(v && typeof v === "object" ? v.value : v);
  return Number.isInteger(n) && n >= 20000 && n <= 150000 ? n : null;
}
function parse(data) {
  if (!data || data.id !== 170087 || !String(data.itemRenderTO?.itemName || "").includes("E7 Pro")) throw new Error("E7 Pro ID/name mismatch");
  const frame = (data.mainItemList || []).find(x => Array.isArray(x.skuExtList) && x.skuExtList.some(s => s.skuCode === "E7WPro"));
  if (!frame) throw new Error("E7 Pro frame SKU group missing");
  const codes = [["E7WPro","ホワイト"],["E7BPro","ブラック"]];
  const variants = codes.map(([code,color]) => {
    const matches = frame.skuExtList.filter(x => x.skuCode === code);
    if (matches.length !== 1) throw new Error("Expected one SKU: " + code);
    const p = price(matches[0].salePrc);
    const regular = price(matches[0].originalPrc);
    if (!p || !regular || p > regular) throw new Error("Invalid sale/original price for " + code);
    return { color, sku_code: code, price_jpy: p, regular_price_jpy: regular };
  });
  const primaryPrc = price(data.shopSkuList?.[0]?.salePrc);
  const min = Math.min(...variants.map(v => v.price_jpy));
  if (primaryPrc != null && primaryPrc !== min) {
    // The product-level total can include a promotion or other adjustment.
    // Never silently substitute a different price for the verified frame-only SKUs.
    throw new Error("SKU min vs product main price mismatch: " + min + "/" + primaryPrc);
  }
  return { variants, min, primaryPrc };
}
async function check(referer) {
  let lastError = null;
  for (let i=0; i<2; i++) {
    try {
      const res = await fetch(API, {
        method:"POST",
        headers:{
          "accept":"application/json",
          "content-type":"application/json",
          "origin":"https://www.flexispot.jp",
          "referer":referer,
          "cache-control":"no-cache",
          "user-agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        },
        body:BODY,
        signal:AbortSignal.timeout(25000)
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const j=await res.json();
      if (j.code !== 200 || j.result !== true) throw new Error("Official API code/result invalid");
      return parse(j.data);
    } catch(e) {
      lastError=String(e);
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }
  throw new Error(lastError || "API failed");
}
const checks = [];
for(const referer of PRODUCT_URLS) {
  try {
    const data = await check(referer);
    checks.push({ referer, ok:true, ...data });
  } catch(e) {
    checks.push({ referer, ok:false, error:String(e).slice(0,350) });
  }
}
const consistent = checks.length === 2 && checks.every(x=>x.ok) &&
  JSON.stringify(checks[0].variants) === JSON.stringify(checks[1].variants);
await fs.writeFile(DIAG, JSON.stringify({schema_version:1, checked_at:checkedAt, source:API, consistent, checks},null,2)+"\n");
if(!consistent) {
  console.error("Official API E7 Pro SKU prices not independently verified:",checks.map(c=>c.error || c.min));
  process.exitCode=1;
} else {
  const verified=checks[0];
  const result={
    schema_version:1,
    ok:true,
    product_id:170087,
    product_name:"FlexiSpot E7 Pro",
    market:"JP",
    currency:"JPY",
    source_kind:"official-item-detail-api",
    checked_at:checkedAt,
    source_urls:PRODUCT_URLS,
    official_api_url:API,
    frame_only:true,
    desktop_type:"無し",
    verified_variants:verified.variants,
    all_colors_verified:true,
    lowest_verified_frame_price_jpy:verified.min,
    checkout_and_coupon_verified:false,
    verification:"Two independent official POST responses with exact frame SKU codes E7WPro/E7BPro and matching SKU prices.",
    notes:"SKU prices are official merchandise sale prices, not verified coupon-adjusted final checkout amounts."
  };
  await fs.writeFile(OUT,JSON.stringify(result,null,2)+"\n");
  console.log(JSON.stringify(result,null,2));
}