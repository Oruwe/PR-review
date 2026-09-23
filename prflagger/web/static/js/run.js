/* The live run view: stages, meters, and the output console.
 *
 * Every number on this page comes from the event stream, and the stream is the
 * same one the transcript is stored as — so a tab opened at the end shows what a
 * tab opened at the start showed. Nothing is computed twice.
 */

import { connect, post } from "/static/js/api.js";

const TERMINAL = new Set(["done", "failed", "cancelled"]);

/* The console can receive tens of thousands of lines. Rendering every one keeps
   the DOM enormous and scrolling janky, so only a trailing window is kept live. */
const WINDOW = 3000;

export function mountRunView({ runId, terminal }) {
  // A run that was already finished when the page opened replays its whole
  // transcript, ending in a `done` event. Redirecting on that would make an
  // old run impossible to read — only a transition that happens while you are
  // watching should move you on to the report.
  const wasFinishedOnLoad = Boolean(terminal);
  const consoleEl = document.getElementById("console");
  const emptyEl = document.getElementById("console-empty");
  const follow = document.getElementById("follow");
  const streamFilter = document.getElementById("stream-filter");
  const searchBox = document.getElementById("log-search");
  const lineCount = document.getElementById("line-count");
  const statePill = document.getElementById("state-pill");
  const elapsedEl = document.getElementById("elapsed");
  const jobLabel = document.getElementById("job-label");
  const argvEl = document.getElementById("argv");

  let lines = 0;
  let startedAt = null;
  let finished = terminal;
  const tests = { passed: 0, failed: 0, total: 0 };

  // -- console ---------------------------------------------------------------

  function appendLine({ stream, text, offset_ms }) {
    emptyEl?.remove();
    const row = document.createElement("div");
    row.className = `row ${stream}`;
    row.dataset.stream = stream;
    row.dataset.text = text.toLowerCase();

    const t = document.createElement("span");
    t.className = "t";
    t.textContent = `${(offset_ms / 1000).toFixed(1)}s`;

    const s = document.createElement("span");
    s.className = "s";
    s.textContent = stream === "stderr" ? "!" : stream === "meta" ? "*" : "·";

    const body = document.createElement("span");
    body.textContent = text;

    row.append(t, s, body);
    consoleEl.append(row);
    lines += 1;

    while (consoleEl.children.length > WINDOW) consoleEl.firstElementChild.remove();
    lineCount.textContent = `${lines.toLocaleString()} line${lines === 1 ? "" : "s"}`;
    applyLogFilters(row);
    if (follow.checked) consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  function applyLogFilters(row) {
    const wanted = streamFilter.value;
    const term = searchBox.value.trim().toLowerCase();
    const matches = (!wanted || row.dataset.stream === wanted)
      && (!term || row.dataset.text.includes(term));
    row.hidden = !matches;
  }

  const refilter = () => {
    for (const row of consoleEl.children) {
      if (row.classList.contains("row")) applyLogFilters(row);
    }
  };
  streamFilter.addEventListener("change", refilter);
  searchBox.addEventListener("input", refilter);
  consoleEl.addEventListener("scroll", () => {
    // Scrolling up is an intent to read; stop yanking the view back down.
    const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
    if (!atBottom && follow.checked) follow.checked = false;
  });

  // -- meters ----------------------------------------------------------------

  function meter(barId, valueId, fraction, label) {
    const bar = document.getElementById(barId);
    const value = document.getElementById(valueId);
    const pct = Math.max(0, Math.min(100, fraction * 100));
    bar.style.width = `${pct}%`;
    // Status colour only when a limit is genuinely close, with the number beside
    // it — the figure carries the meaning, the colour only draws the eye.
    bar.style.background = pct >= 90 ? "var(--critical)"
      : pct >= 75 ? "var(--serious)" : "var(--seq-400)";
    value.textContent = label;
  }

  function onSample(event) {
    meter("cpu-bar", "cpu-val", (event.cpu_pct || 0) / 100, `${(event.cpu_pct || 0).toFixed(0)}%`);
    const limit = event.memory_mb || 1024;
    meter("rss-bar", "rss-val", (event.rss_mb || 0) / limit,
      `${event.rss_mb || 0}/${limit} MB`);
    meter("pid-bar", "pid-val", (event.pids || 0) / 256, `${event.pids || 0}/256`);
  }

  function onTests(event) {
    tests.total = event.tests || 0;
    tests.passed = event.passed || 0;
    tests.failed = event.failed || 0;
    const bar = document.getElementById("test-bar");
    const value = document.getElementById("test-val");
    const detail = document.getElementById("test-detail");
    if (!tests.total) { value.textContent = "—"; return; }
    const pass = (tests.passed / tests.total) * 100;
    const fail = (tests.failed / tests.total) * 100;
    bar.innerHTML = "";
    if (pass > 0) bar.insertAdjacentHTML("beforeend",
      `<span style="width:${pass}%;background:var(--good)"></span>`);
    if (fail > 0) bar.insertAdjacentHTML("beforeend",
      `<span style="width:${fail}%;background:var(--critical)"></span>`);
    value.textContent = `${tests.passed}/${tests.total}`;
    detail.textContent = `${tests.passed} passed, ${tests.failed} failed `
      + `in ${event.stage.replace(/_/g, " ")}`;
  }

  // -- stages ----------------------------------------------------------------

  const order = [...document.querySelectorAll(".stage")].map((el) => el.dataset.stage);

  function onState(event) {
    const state = event.state;
    statePill.textContent = state.replace(/_/g, " ");
    statePill.classList.toggle("live", !TERMINAL.has(state));
    const index = order.indexOf(state);
    document.querySelectorAll(".stage").forEach((el, position) => {
      el.classList.remove("done", "active", "failed");
      if (state === "failed" || state === "cancelled") {
        if (position <= Math.max(index, 0)) el.classList.add("failed");
      } else if (index >= 0 && position < index) el.classList.add("done");
      else if (index >= 0 && position === index) {
        el.classList.add(state === "done" ? "done" : "active");
      }
    });
    if (state === "done") document.querySelectorAll(".stage").forEach((el) => el.classList.add("done"));
    if (TERMINAL.has(state)) {
      finished = true;
      document.getElementById("cancel").disabled = true;
      jobLabel.textContent = state;
      if (state === "done" && !wasFinishedOnLoad) {
        setTimeout(() => { location.href = `/runs/${runId}/report`; }, 1500);
      }
    }
  }

  // -- elapsed ---------------------------------------------------------------

  setInterval(() => {
    if (!startedAt || finished) return;
    const seconds = (Date.now() - startedAt) / 1000;
    elapsedEl.textContent = `${seconds.toFixed(0)}s elapsed`;
  }, 500);

  // -- the stream ------------------------------------------------------------

  connect(`/ws/runs/${runId}`, (event) => {
    switch (event.type) {
      case "log.line":
        appendLine(event);
        break;
      case "job.sample":
        onSample(event);
        break;
      case "job.started":
        startedAt = startedAt || Date.now();
        jobLabel.textContent = (event.stage || "").replace(/_/g, " ");
        argvEl.textContent = (event.argv || []).join(" ");
        break;
      case "job.building":
        jobLabel.textContent = `building image for ${event.pack || "repo"}`;
        break;
      case "job.finished":
        if (event.tests) onTests(event);
        break;
      case "run.state":
        startedAt = startedAt || Date.now();
        onState(event);
        break;
      case "run.toolchain":
        jobLabel.textContent = event.display || event.pack;
        break;
      default:
        break;
    }
  });

  document.getElementById("cancel").addEventListener("click", async (clicked) => {
    clicked.currentTarget.disabled = true;
    try { await post(`/api/runs/${runId}/cancel`, {}); } catch { /* already gone */ }
  });
}
