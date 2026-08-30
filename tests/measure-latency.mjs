// Glass-to-glass latency of an fpgas.online camera stream as a browser sees it.
//
// Opens a page that hosts the site's video.js player (a real board page such as
// https://tinytapeout.fpgas.online/board/tt04/, or tests/ci/player.html), waits
// for playback, then repeatedly draws the current video frame to a canvas,
// OCRs the clockoverlay the Pi burns into the top-left corner (tesseract) and
// compares it with the wall clock. Also reports the structural latency video.js
// can see on its own: how far behind the newest *listed* HLS fragment it plays.
//
// Needs an H.264-capable browser: Playwright's bundled Chromium has no H.264
// (video.js reports MEDIA_ERR_SRC_NOT_SUPPORTED), so we drive a system
// Chrome/Chromium ($CHROMIUM, default /usr/bin/chromium) and tesseract-ocr.
//
//   node tests/measure-latency.mjs URL [--samples N] [--max-latency S]
//        [--source-tz Europe/London] [--video '#tt-video'] [--json out.json]
//
// --source-tz is the timezone of the clock in the picture (the Pi's local
// zone); default is this machine's zone, right for the CI harness where the
// encoder and the browser share a host. Exit status 1 if the median measured
// latency exceeds --max-latency (default: no limit), 2 on failure to play.
import { chromium } from 'playwright-core';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
let url = null;
for (let i = 0; i < args.length; i++) { if (args[i].startsWith('--')) i++; else url = args[i]; }
const samples = Number(opt('--samples', 5));
const maxLatency = opt('--max-latency', null) === null ? null : Number(opt('--max-latency'));
const sourceTz = opt('--source-tz', Intl.DateTimeFormat().resolvedOptions().timeZone);
const videoSel = opt('--video', 'video.video-js');
const jsonOut = opt('--json', null);
const playTimeout = Number(opt('--play-timeout', 90));
if (!url) { console.error('usage: measure-latency.mjs URL [options]'); process.exit(2); }

const log = (...a) => console.error(new Date().toISOString(), ...a);
const workdir = mkdtempSync(join(tmpdir(), 'cam-latency-'));

// Wall clock in the source's zone as seconds since local midnight.
function nowInSourceTz(ms) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: sourceTz, hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }).formatToParts(new Date(ms));
  const get = (t) => Number(parts.find((p) => p.type === t).value);
  return (get('hour') % 24) * 3600 + get('minute') * 60 + get('second') + (ms % 1000) / 1000;
}

function ocrClock(png) {
  const file = join(workdir, `clock-${Date.now()}.png`);
  writeFileSync(file, png);
  const text = execFileSync('tesseract', [file, '-', '--psm', '7', '-c', 'tessedit_char_whitelist=0123456789:'], { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  const m = text.match(/(\d{1,2}):(\d{2}):(\d{2})/);
  if (!m) return { text, file, secs: null };
  return { text, file, secs: Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) };
}

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM || '/usr/bin/chromium', headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
page.on('console', (m) => { if (m.type() === 'error') log('browser console:', m.text()); });
await page.goto(url, { waitUntil: 'load' });
const support = await page.evaluate(() => MediaSource.isTypeSupported('video/mp4; codecs="avc1.64002a"'));
log('H.264 via MSE supported:', support);

// The site's video.js instance: <video class="video-js"> gets a .player property.
const probe = () => page.evaluate((sel) => {
  const v = document.querySelector(sel); const p = v && v.player;
  if (!p) return { err: 'no video.js player on ' + sel };
  const el = p.tech_ && p.tech_.el_;
  const out = { now: Date.now(), paused: p.paused(), readyState: p.readyState(), currentTime: p.currentTime(), w: el && el.videoWidth, h: el && el.videoHeight };
  try { const q = el.getVideoPlaybackQuality(); out.frames = q.totalVideoFrames; out.dropped = q.droppedVideoFrames; } catch (e) { }
  try {
    const pl = p.tech_.vhs.playlists.media();
    out.targetDuration = pl.targetDuration; out.mediaSequence = pl.mediaSequence; out.nSegs = pl.segments.length;
    out.lastSeg = pl.segments[pl.segments.length - 1].uri; out.lastSegDuration = pl.segments[pl.segments.length - 1].duration;
  } catch (e) { out.vhsErr = String(e); }
  try { const sk = p.seekable(); out.seekableEnd = sk.length ? sk.end(sk.length - 1) : null; } catch (e) { }
  return out;
}, videoSel);

// Wait until frames are actually being decoded.
let state; const t0 = Date.now();
for (;;) {
  state = await probe();
  if (state.err) { log(state.err); await browser.close(); process.exit(2); }
  if (state.frames > 0 && !state.paused && state.readyState >= 3) break;
  if (Date.now() - t0 > playTimeout * 1000) { log('gave up waiting for playback:', JSON.stringify(state)); await browser.close(); process.exit(2); }
  await page.waitForTimeout(1000);
}
log('playing after', ((Date.now() - t0) / 1000).toFixed(1), 's:', JSON.stringify(state));
await page.waitForTimeout(3000); // let the initial buffer settle before sampling

const results = [];
for (let i = 0; i < samples; i++) {
  // Grab the clock region of the *displayed* frame, upscaled 3x for tesseract.
  const shot = await page.evaluate((sel) => {
    const v = document.querySelector(sel).player.tech_.el_;
    const sw = Math.round(v.videoWidth * 0.3), sh = Math.round(v.videoHeight * 0.12);
    const c = document.createElement('canvas'); c.width = sw * 3; c.height = sh * 3;
    c.getContext('2d').drawImage(v, 0, 0, sw, sh, 0, 0, c.width, c.height);
    return { now: Date.now(), png: c.toDataURL('image/png').split(',')[1] };
  }, videoSel);
  const st = await probe();
  const ocr = ocrClock(Buffer.from(shot.png, 'base64'));
  const r = { sample: i, at: new Date(shot.now).toISOString(), ocr: ocr.text, currentTime: st.currentTime, targetDuration: st.targetDuration, dropped: st.dropped };
  if (ocr.secs !== null) {
    let d = nowInSourceTz(shot.now) - ocr.secs;
    if (d < -43200) d += 86400; if (d > 43200) d -= 86400; // midnight wrap
    r.latency_s = Number(d.toFixed(2));
  } else { r.latency_s = null; r.ocrFile = ocr.file; }
  // Structural: with hls_fragment_naming system the segment name carries its open time (ms).
  const m = st.lastSeg && st.lastSeg.match(/-(\d{13})\.ts$/);
  if (m) r.newest_listed_fragment_age_s = Number(((shot.now - Number(m[1])) / 1000 - (st.lastSegDuration || 0)).toFixed(2));
  results.push(r);
  log(JSON.stringify(r));
  await page.waitForTimeout(2000);
}
await browser.close();

const good = results.map((r) => r.latency_s).filter((x) => x !== null).sort((a, b) => a - b);
const median = good.length ? good[Math.floor(good.length / 2)] : null;
const summary = { url, sourceTz, samples: results.length, ocr_ok: good.length, median_latency_s: median, min_latency_s: good[0] ?? null, max_latency_s: good[good.length - 1] ?? null, targetDuration: state.targetDuration, results };
console.log(JSON.stringify(summary, null, 2));
if (jsonOut) writeFileSync(jsonOut, JSON.stringify(summary, null, 2));
if (good.length < Math.ceil(results.length / 2)) { log('OCR failed on most samples; crops kept in', workdir); process.exit(2); }
if (maxLatency !== null && median > maxLatency) { log(`FAIL: median latency ${median}s > ${maxLatency}s`); process.exit(1); }
log(`median glass-to-glass latency ${median}s (${good.length}/${results.length} samples OCR'd)`);
