"use strict";

const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { resolve } = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
  const [, , url, frames] = process.argv;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE_PATH });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", (route) => {
      const target = new URL(route.request().url());
      if (route.request().method() !== "GET") {
        errors.push(`Unexpected capture action: ${route.request().method()} ${target.pathname}`);
        return route.abort();
      }
      return target.origin === new URL(url).origin ? route.continue() : route.abort();
    });
    await page.goto(url);
    await page.locator(".run-row").first().waitFor();
    await page.locator("#demo-banner").waitFor({ state: "visible" });
    await page.evaluate(() => document.fonts.ready);
    await page.setViewportSize({ width: 1440, height: 1040 });
    assert.equal(await page.locator(".app-note").count(), 0);
    const statuses = await page.locator(".run-row:not(.sub) .status").evaluateAll((cells) => cells.map((cell) => {
      const glyph = cell.querySelector(".glyph").getBoundingClientRect();
      const text = [...cell.childNodes].find((node) => node.nodeType === Node.TEXT_NODE);
      const range = document.createRange();
      range.setStart(text, 1); range.setEnd(text, text.textContent.length);
      return { label: text.textContent.trim(), glyph: glyph.x, text: range.getBoundingClientRect().x };
    }));
    assert.equal(new Set(statuses.map((status) => status.glyph)).size, 1, "Status symbols must align");
    assert.equal(new Set(statuses.map((status) => status.text)).size, 1, "Status labels must align");
    const row = (id) => page.locator(`.run-row[data-run-id="${id}"]`);
    for (const id of ["media-claude:reconnect", "media-opencode:upload", "media-pi:json"]) {
      assert.match(await row(id).innerText(), /running/);
      assert.doesNotMatch(await row(id).innerText(), /your turn/);
    }
    await row("media-claude:reconnect").click();
    assert.equal(await page.locator("#detail .rest-link").count(), 0);
    await page.keyboard.press("Escape");
    await row("media-codex:tokens").click();
    await page.locator("#detail").getByRole("button", { name: "let it rest" }).click();
    await page.keyboard.press("Escape");
    await row("media-codex:orders").click();
    const actions = async () => {
      const open = page.locator("#detail").getByRole("button", { name: "open in Ghostty" });
      const rest = page.locator("#detail").getByRole("button", { name: "let it rest" });
      await open.waitFor({ state: "visible" });
      await rest.waitFor({ state: "visible" });
      assert.equal(await open.isEnabled(), true);
      assert.equal(await rest.isEnabled(), true);
    };
    await actions();
    await page.locator(".brand").click();
    await page.screenshot({ path: resolve("docs/dashboard.png"), fullPage: true });
    await page.setViewportSize({ width: 1100, height: 800 });
    await page.locator("#search").fill("Orders cursor pagination");
    await actions();
    await page.locator(".brand").click();
    await page.locator(".agents").screenshot({ path: resolve("docs/session-open.png") });
    await page.locator("#search").fill("");
    await page.locator("#detail").getByRole("button", { name: "Close", exact: true }).click();
    await page.setViewportSize({ width: 1280, height: 800 });
    for (let frame = 0; frame < 50; frame++) {
      if (frame === 10) await page.locator('[data-view="waiting"]').click();
      if (frame === 17) { await row("media-codex:orders").click(); await actions(); }
      if (frame === 25) await page.locator("#detail").getByRole("button", { name: "let it rest" }).click();
      if (frame === 29) await page.keyboard.press("Escape");
      if (frame === 32) await page.locator('[data-view="active"]').click();
      if (frame === 37) await page.locator("#search").fill("reconnect");
      if (frame === 44) await page.locator("#search").fill("");
      await page.waitForTimeout(100);
      await page.screenshot({ path: resolve(frames, `frame-${String(frame).padStart(3, "0")}.png`) });
    }
    await page.unroute("**/*");
    await page.route("**/*", (route) => new URL(route.request().url()).protocol === "file:" ? route.continue() : route.abort());
    await page.setViewportSize({ width: 1280, height: 640 });
    await page.goto(pathToFileURL(resolve("docs/media/social-preview.html")).href);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: resolve("docs/media/social-preview.png") });
    if (errors.length) throw new Error(`Browser errors: ${errors.join("; ")}`);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
