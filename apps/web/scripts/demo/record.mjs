#!/usr/bin/env node
// Records the README demo by driving the real UI of a running stack, from an empty workspace to a
// cited answer. Nothing is staged: the file is uploaded through "Add data", read by the Mapper,
// ingested, explored in the graph and questioned in the AI Assistant — every step a real click.
//
//   node scripts/demo/record.mjs            reset the workspace's data, record, encode
//   node scripts/demo/record.mjs --no-reset keep the current data (a rehearsal after a take)
//   node scripts/demo/record.mjs --encode   only re-encode the last take (to tune the edit)
//
// Env: OPENCORTYX_ADMIN_EMAIL / OPENCORTYX_ADMIN_PASSWORD (an admin of the workspace), DEMO_WEB
// (default http://localhost:3000), DEMO_API (default http://localhost:8000/api/v2), DEMO_QUESTION,
// DEMO_WORKSPACE (the workspace name shown at the top of the graph, default "Atlas Group").
//
// The reset deletes this workspace's data, saved ingestion jobs, schema mappings, chat history and
// the pipelines earlier takes drafted from the demo file. Users, API keys and settings stay, except
// the workspace name, which it sets. It exists so every take starts from the same empty state.
//
// Capture: Chrome's screencast at 2x device pixels (sharper than a screen recording), one JPEG per
// repaint with its timestamp. Long waits (reading, ingesting, the model answering) are marked while
// recording and sped up in the edit, with a "sped up" badge on screen so nothing pretends to be
// faster than it is. Encoding runs ffmpeg in a throwaway container: MP4 for the docs, GIF for the
// README header. Output: docs/assets/demo/opencortyx-demo.{mp4,gif}.
import { chromium } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../../..");
const OUT = path.join(HERE, "out");
const FRAMES = path.join(OUT, "frames");
const ASSETS = path.join(REPO, "docs/assets/demo");
const FILE = path.join(HERE, "atlas-programme.json");
const STILLS = path.join(REPO, "docs/assets/screenshots");   // docs screenshots taken during the take

const WEB = process.env.DEMO_WEB || "http://localhost:3000";
const API = process.env.DEMO_API || "http://localhost:8000/api/v2";
const EMAIL = process.env.OPENCORTYX_ADMIN_EMAIL;
const PASSWORD = process.env.OPENCORTYX_ADMIN_PASSWORD;
const QUESTION = process.env.DEMO_QUESTION
  || "Who leads the Payments Core Rewrite, and what is its biggest risk?";
const TAG = "portfolio";
const WORKSPACE = process.env.DEMO_WORKSPACE || "Atlas Group";

const W = 1440, H = 900, DPR = 2;          // what the viewer sees, and the capture density
const FPS = 30;
const MAX_HOLD = 4;                        // seconds one unchanged frame may stay on screen
const GIF_SPEED = 1.5;                     // the README GIF plays faster than the MP4

const args = new Set(process.argv.slice(2));

// ── reset: the same empty workspace for every take ─────────────────────────────────────────────
async function api(method, url, token, body) {
  const r = await fetch(`${API}${url}`, {
    method, headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${method} ${url} → ${r.status} ${await r.text()}`);
  return r.status === 204 ? null : r.json();
}

async function resetWorkspace() {
  const { access_token: t } = await api("POST", "/auth/token", null, { username: EMAIL, password: PASSWORD });
  const list = (d) => (Array.isArray(d) ? d : d.items || d.ingestions || d.mappings || []);
  for (const i of list(await api("GET", "/ingestions", t))) await api("DELETE", `/ingestions/${i.id}`, t);
  for (const m of list(await api("GET", "/schema-mappings?light=true", t))) await api("DELETE", `/schema-mappings/${m.id}`, t);
  for (const c of list(await api("GET", "/chats?limit=200", t))) await api("DELETE", `/chats/${c.id}`, t);
  // "Use this reading" drafts a pipeline per take; only the ones drafted from the demo file go
  const drafted = `Drafted from ${path.basename(FILE)}`;
  for (const p of list(await api("GET", "/pipelines", t)))
    if (!p.is_system_default && String(p.description || "").startsWith(drafted)) await api("DELETE", `/pipelines/${p.id}`, t);
  await api("DELETE", "/reset", t);
  await api("PUT", "/config", t, { values: { workspace_name: WORKSPACE } });   // the top of the graph
  console.log(`workspace reset: data, ingestion jobs, mappings and chat history deleted; named "${WORKSPACE}"`);
}

// ── on-screen overlay: cursor, click ripple, captions, title cards, the "sped up" badge ──────────
// Installed on demand rather than at document start: the app router hydrates <html>, and a node
// added before hydration would be a mismatch. `ensure()` reinstalls it after any full navigation.
function installOverlay() {
  if (document.getElementById("__demo")) return;
  const root = document.createElement("div");
  root.id = "__demo";
  root.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  root.innerHTML = `
    <style>
      #__demo .cur{position:absolute;left:0;top:0;width:26px;height:26px;filter:drop-shadow(0 2px 3px rgba(0,0,0,.35))}
      #__demo .rip{position:absolute;width:40px;height:40px;margin:-20px 0 0 -20px;border-radius:50%;
        border:3px solid rgba(20,184,166,.95);animation:__rip .55s ease-out forwards}
      @keyframes __rip{from{transform:scale(.25);opacity:1}to{transform:scale(1.5);opacity:0}}
      #__demo .cap{position:absolute;left:50%;bottom:32px;transform:translate(-50%,8px);display:flex;gap:14px;
        align-items:center;background:rgba(15,23,42,.9);color:#fff;padding:12px 22px 12px 12px;border-radius:16px;
        box-shadow:0 12px 32px rgba(15,23,42,.28);opacity:0;transition:opacity .35s,transform .35s;
        font:600 18px/1.25 Inter,ui-sans-serif,system-ui,sans-serif;white-space:nowrap}
      #__demo .cap.on{opacity:1;transform:translate(-50%,0)}
      #__demo .cap.top{bottom:auto;top:84px}
      #__demo .cap b{background:#14b8a6;border-radius:10px;min-width:34px;height:34px;display:flex;
        align-items:center;justify-content:center;font-size:16px}
      #__demo .cap small{display:block;font-weight:500;font-size:13.5px;color:#cbd5e1;margin-top:3px}
      #__demo .ff{position:absolute;top:84px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.85);color:#fff;
        font:600 13px Inter,system-ui,sans-serif;padding:7px 12px;border-radius:9px;opacity:0;transition:opacity .2s}
      #__demo .card{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
        background:radial-gradient(circle at 50% 38%,#134e4a 0%,#0f172a 62%);color:#fff;opacity:0;transition:opacity .6s;
        font-family:Inter,ui-sans-serif,system-ui,sans-serif;text-align:center}
      #__demo .card.on{opacity:1}
      #__demo .card h1{font-size:58px;font-weight:800;letter-spacing:-1.5px;margin:0}
      #__demo .card p{font-size:24px;font-weight:500;color:#99f6e4;margin:14px 0 0}
      #__demo .card i{font-style:normal;font-size:16px;color:#94a3b8;margin-top:26px;letter-spacing:.3px}
    </style>
    <div class="card"></div>
    <div class="cap"><b></b><div><span></span><small></small></div></div>
    <div class="ff">⏩ sped up</div>
    <svg class="cur" viewBox="0 0 24 24"><path d="M4 2.5l15.5 9.2-6.9 1.5 4.4 7.9-2.9 1.6-4.4-7.9L4.2 19.8z"
      fill="#0f172a" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  document.documentElement.appendChild(root);
  const cur = root.querySelector(".cur");
  const place = (x, y) => { cur.style.transform = `translate(${x - 4}px,${y - 3}px)`; };
  addEventListener("mousemove", (e) => place(e.clientX, e.clientY), true);
  addEventListener("mousedown", (e) => {
    const r = document.createElement("div");
    r.className = "rip"; r.style.left = `${e.clientX}px`; r.style.top = `${e.clientY}px`;
    root.appendChild(r); setTimeout(() => r.remove(), 700);
  }, true);
  window.__demo = {
    place,
    caption(n, text, sub, top) {
      const c = root.querySelector(".cap");
      if (!text) { c.classList.remove("on"); return; }
      c.classList.toggle("top", !!top);
      c.querySelector("b").textContent = n;
      c.querySelector("span").textContent = text;
      const s = c.querySelector("small"); s.textContent = sub || ""; s.style.display = sub ? "block" : "none";
      c.classList.add("on");
    },
    ff(on) { root.querySelector(".ff").style.opacity = on ? "1" : "0"; },
    card(html) {
      const c = root.querySelector(".card");
      cur.style.visibility = html ? "hidden" : "visible";      // no cursor on a title card
      if (!html) { c.classList.remove("on"); return; }
      c.innerHTML = html; c.classList.add("on");
    },
  };
}

// ── the director: mouse, captions and the speed timeline ───────────────────────────────────────
class Director {
  constructor(page) {
    this.page = page;
    this.pos = { x: W * 0.55, y: H * 0.45 };
    this.caption = null;
    this.speeds = [{ t: Date.now() / 1000, factor: 1 }];
    this.cuts = [];                        // [from, to] seconds whose frames the edit drops
  }
  /** A docs screenshot of the current screen, without the overlay. The moment the overlay is hidden
   *  is cut from the video, so the take does not flicker. */
  async still(name) {
    const from = Date.now() / 1000;
    await this.page.evaluate(() => { const o = document.getElementById("__demo"); if (o) o.style.display = "none"; });
    await this.page.waitForTimeout(250);
    await this.page.screenshot({ path: path.join(STILLS, `${name}.png`), scale: "css" });
    await this.page.evaluate(() => { const o = document.getElementById("__demo"); if (o) o.style.display = ""; });
    await this.page.waitForTimeout(250);
    this.cuts.push([from, Date.now() / 1000]);
  }
  async ensure() {
    await this.page.evaluate(installOverlay);
    await this.page.evaluate(({ x, y }) => window.__demo.place(x, y), this.pos);
    if (this.caption) await this.page.evaluate((c) => window.__demo.caption(...c), this.caption);
  }
  /** Caption for a step; `top` when the bottom of the screen is where the action is (the chat box). */
  async say(n, text, sub = "", top = false) {
    this.caption = [String(n), text, sub, top];
    await this.ensure();
  }
  async card(html) { await this.ensure(); await this.page.evaluate((h) => window.__demo.card(h), html); }
  async speed(factor) {
    this.speeds.push({ t: Date.now() / 1000, factor });
    await this.ensure();
    await this.page.evaluate((on) => window.__demo.ff(on), factor > 1.5);
  }
  async hold(ms) { await this.page.waitForTimeout(ms); }
  async glide(x, y, ms = 750) {
    const { x: sx, y: sy } = this.pos;
    const steps = Math.max(14, Math.round(ms / 16));
    for (let i = 1; i <= steps; i++) {
      const k = i / steps, e = k < 0.5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2;   // ease in-out
      await this.page.mouse.move(sx + (x - sx) * e, sy + (y - sy) * e);
      await this.page.waitForTimeout(ms / steps);
    }
    this.pos = { x, y };
  }
  async point(locator, ms = 750) {
    await locator.scrollIntoViewIfNeeded();
    const b = await locator.boundingBox();
    if (!b) throw new Error(`not visible: ${locator}`);
    await this.glide(b.x + b.width / 2, b.y + b.height / 2, ms);
  }
  async click(locator, { ms = 750, before = 220, after = 500 } = {}) {
    await this.point(locator, ms);
    await this.hold(before);
    await this.page.mouse.down(); await this.hold(90); await this.page.mouse.up();
    await this.hold(after);
    await this.ensure();                 // a click may have navigated
  }
  async type(text, delay = 55) { await this.page.keyboard.type(text, { delay }); }
  async scroll(dy, ms = 1200) {
    const steps = Math.max(10, Math.round(ms / 30));
    for (let i = 0; i < steps; i++) { await this.page.mouse.wheel(0, dy / steps); await this.page.waitForTimeout(ms / steps); }
  }
  /** Wait for `until`, sped up in the edit and badged on screen. */
  async fastForward(until, factor = 8, timeout = 300_000) {
    await this.speed(factor);
    try { await until.waitFor({ state: "visible", timeout }); } finally { await this.speed(1); }
  }
}

// ── the take ────────────────────────────────────────────────────────────────────────────────────
async function record() {
  if (!EMAIL || !PASSWORD) throw new Error("set OPENCORTYX_ADMIN_EMAIL and OPENCORTYX_ADMIN_PASSWORD");
  if (!args.has("--no-reset")) await resetWorkspace();
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(FRAMES, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: DPR });
  const page = await context.newPage();

  // Off camera: sign in and land on the (empty) catalogue.
  await page.goto(`${WEB}/auth/login`);
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/dashboard/);
  await page.goto(`${WEB}/dashboard/catalogue`);
  await page.waitForLoadState("networkidle");

  const d = new Director(page);
  await d.ensure();

  const frames = [];
  const cdp = await context.newCDPSession(page);
  cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
    const name = `${String(frames.length).padStart(6, "0")}.jpg`;
    fs.writeFileSync(path.join(FRAMES, name), Buffer.from(data, "base64"));
    frames.push({ name, t: metadata.timestamp });
    try { await cdp.send("Page.screencastFrameAck", { sessionId }); } catch { /* closing */ }
  });
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: W * DPR, maxHeight: H * DPR });

  const nav = (label) => page.getByRole("link", { name: label, exact: true }).first();

  try {
    // 1 · Add a file
    await d.say(1, "Add a JSON file", "Documents, spreadsheets, JSON, CSV, databases — one drop zone");
    await d.hold(700);
    await d.click(nav("Add data"));
    await page.getByText("Drop a file here, or click to choose one").waitFor();
    await d.hold(600);
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      d.click(page.getByText("Drop a file here, or click to choose one"), { after: 200 }),
    ]);
    await chooser.setFiles(FILE);
    await d.fastForward(page.getByText(/Here is how it would read/), 4, 180_000);
    await d.ensure();

    // 2 · The Mapper drafts a reading
    await d.say(2, "The Mapper drafts how to read it", "Every decision carries the measurement behind it");
    await d.hold(1400);
    await d.glide(W * 0.52, H * 0.55, 600);
    await d.scroll(520, 2600);
    await d.hold(900);
    const tags = page.locator("#add-tags");
    await d.click(tags);
    await d.type(TAG, 70);
    await page.keyboard.press("Enter");
    await d.hold(700);

    // 3 · Ingest
    await d.say(3, "Ingest it — every field becomes a node or an edge");
    await d.click(page.getByRole("button", { name: /Use this reading and add it/ }));
    await d.fastForward(page.getByText(/Pull something else out of this/), 10, 600_000);
    await d.hold(800);
    await d.glide(W * 0.52, H * 0.5, 500);
    await d.scroll(-2000, 400);
    await d.hold(1200);
    await d.scroll(700, 3000);
    await d.hold(1500);

    // 4 · The reading is now a saved mapping
    await d.say(4, "The reading is saved as a reusable mapping", "Correct it once, every re-run follows it");
    await d.click(nav("Mapper"));
    const mapping = page.locator('a[href^="/dashboard/mapper/"]').first();
    await mapping.waitFor({ timeout: 30_000 });
    await d.hold(900);
    await d.click(mapping);
    await page.waitForURL(/\/dashboard\/mapper\/.+/);
    await page.waitForLoadState("networkidle");
    await d.hold(1300);
    await d.glide(W * 0.55, H * 0.55, 600);
    await d.scroll(600, 2800);
    await d.hold(1400);

    // 5 · Explore the graph
    await d.say(5, "Explore the knowledge graph", "Workspace → source → tag → asset → what it contains");
    await d.click(nav("Lineage"));
    const svg = page.locator('svg[aria-label="Knowledge graph"]');
    await d.fastForward(svg, 6, 180_000);
    await d.hold(1200);
    const item = (re) => svg.locator("g").filter({ has: page.locator(":scope > title", { hasText: re }) }).first();
    const open = async (re, wait = 1600) => {
      const it = item(re);
      await it.waitFor({ timeout: 60_000 });
      await d.click(it, { ms: 850, after: wait });
    };
    await open(/click to open/);                           // the workspace root
    await open(/^Files|^Uploads|click to open/);          // the source
    await open(new RegExp(TAG));                           // the tag the file was given
    await open(/atlas-programme/, 2200);                   // the asset
    await open(/Payments Core Rewrite/, 2400);             // one of its records → its relationships
    await d.glide(W * 0.42, H * 0.5, 600);
    await page.mouse.wheel(0, -160); await d.hold(260);                                     // zoom in a little
    await d.hold(1400);
    await d.still("lineage");

    // 6 · Search
    await d.say(6, "Search highlights it wherever it lives");
    await d.click(page.getByLabel("Search the graph"));
    await d.type("Northwind", 80);
    await page.keyboard.press("Enter");
    await d.hold(2800);

    // 7 · Ask
    await d.say(7, "Ask the AI Assistant — every answer cites its source", "", true);
    await d.click(nav("AI Assistant"));
    const ask = page.getByPlaceholder("Ask a question...");
    await ask.waitFor();
    await d.click(ask);
    await d.type(QUESTION, 45);
    await d.hold(400);
    await d.click(page.getByRole("button", { name: /^Ask$/ }));
    await d.fastForward(page.getByText(/^Sources$/i).first(), 3, 240_000);
    await d.hold(1200);
    const firstSource = page.locator('a[href^="/dashboard/catalogue/"]').filter({ hasText: /atlas-programme/ }).first();
    if (await firstSource.count()) { await d.point(firstSource, 900); await d.hold(2600); }
    else await d.hold(2600);
    await d.still("ai-assistant");

    // Close
    await d.say(null, "");
    await d.card(`<h1>OpenCortyx</h1><p>The Knowledge Layer for AI</p><i>Self-hosted · MCP-ready · opencortyx.com</i>`);
    await d.hold(3600);
  } finally {
    await cdp.send("Page.stopScreencast").catch(() => {});
    await page.waitForTimeout(300);
    fs.writeFileSync(path.join(OUT, "timeline.json"), JSON.stringify({ frames, speeds: d.speeds, cuts: d.cuts }, null, 1));
    await page.screenshot({ path: path.join(OUT, "last.png") }).catch(() => {});
    await browser.close();
  }
  console.log(`recorded ${frames.length} frames`);
}

// ── the edit: re-time the frames, then encode ───────────────────────────────────────────────────
function encode() {
  const timeline = JSON.parse(fs.readFileSync(path.join(OUT, "timeline.json"), "utf8"));
  const cut = (t) => (timeline.cuts || []).some(([a, b]) => t >= a && t <= b + 0.1);
  const frames = timeline.frames.filter((f) => !cut(f.t));
  const speeds = timeline.speeds;
  if (!frames.length) throw new Error("no frames recorded");
  const factorAt = (t) => { let f = 1; for (const s of speeds) if (s.t <= t) f = s.factor; return f; };
  // Place every frame on the edited timeline (each gap scaled by its speed, long stills capped),
  // then sample that timeline at exactly FPS. Giving each frame a minimum length instead stretched
  // the video: the browser paints faster than 30 fps during animations, so 5,374 frames of a
  // 157-second take came out at 227 seconds.
  const at = [0];
  for (let i = 1; i < frames.length; i++) {
    const gap = (frames[i].t - frames[i - 1].t) / factorAt(frames[i - 1].t);
    at.push(at[i - 1] + Math.min(MAX_HOLD, gap));
  }
  const total = at[at.length - 1] + 1.5;                                   // hold the last frame
  const picks = [];
  for (let k = 0, j = 0; k / FPS < total; k++) {
    while (j + 1 < frames.length && at[j + 1] <= k / FPS) j++;
    if (picks.length && picks[picks.length - 1].j === j) picks[picks.length - 1].n++;
    else picks.push({ j, n: 1 });
  }
  const lines = ["ffconcat version 1.0"];
  for (const p of picks) lines.push(`file 'frames/${frames[p.j].name}'`, `duration ${(p.n / FPS).toFixed(4)}`);
  lines.push(`file 'frames/${frames[picks[picks.length - 1].j].name}'`);  // ffconcat needs the last one twice
  fs.writeFileSync(path.join(OUT, "frames.txt"), lines.join("\n") + "\n");
  console.log(`edit: ${frames.length} frames → ${total.toFixed(1)} s`);

  fs.mkdirSync(ASSETS, { recursive: true });
  const docker = (script) => execFileSync("docker", [
    "run", "--rm", "-v", `${OUT}:/w`, "-v", `${ASSETS}:/assets`, "-w", "/w", "python:3.11-slim", "sh", "-c",
    `pip install -q --root-user-action=ignore imageio-ffmpeg >/dev/null 2>&1 && ` +
    `F=$(python -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())') && ${script}`,
  ], { stdio: "inherit", env: { ...process.env, MSYS_NO_PATHCONV: "1" } });

  docker(
    `$F -y -loglevel error -f concat -safe 0 -i frames.txt ` +
    `-vf "fps=${FPS},scale=1920:-2:flags=lanczos,format=yuv420p" -c:v libx264 -crf 18 -preset slow ` +
    `-movflags +faststart /assets/opencortyx-demo.mp4 && ` +
    // The README GIF is built from the frames, not by decoding the MP4 (that ran the Docker VM out
    // of memory with the model server loaded), and plays at GIF_SPEED so it stays light enough to
    // load on a repository page; the MP4 is the full-speed, full-quality cut.
    `$F -y -loglevel error -f concat -safe 0 -i frames.txt ` +
    `-vf "setpts=PTS/${GIF_SPEED},fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff:max_colors=128[p];` +
    `[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" /assets/opencortyx-demo.gif`,
  );
  console.log(`mp4 ${total.toFixed(1)} s · gif ${(total / GIF_SPEED).toFixed(1)} s`);
  for (const f of ["opencortyx-demo.mp4", "opencortyx-demo.gif"]) {
    console.log(`${f}: ${(fs.statSync(path.join(ASSETS, f)).size / 1e6).toFixed(1)} MB`);
  }
}

if (!args.has("--encode")) await record();
encode();
