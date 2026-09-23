/* Talking to the service: fetch helpers and the event socket.
 *
 * `connect` reconnects with the cursor it last saw, so a dropped socket resumes
 * the transcript rather than restarting it or losing the middle.
 */

/** A signed-out session goes back to the sign-in page, then returns here. */
function signInAgain() {
  const here = location.pathname + location.search;
  location.assign(`/login?next=${encodeURIComponent(here)}`);
}

export async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (response.status === 401) { signInAgain(); throw new Error("signed out"); }
  const text = await response.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!response.ok) {
    throw new Error((parsed && (parsed.detail || parsed.error)) || text || response.statusText);
  }
  return parsed;
}

export async function get(path) {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 401) { signInAgain(); throw new Error("signed out"); }
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

const indicator = () => document.getElementById("conn");

function setConnection(state) {
  const pill = indicator();
  if (!pill) return;
  pill.hidden = state === "live";
  pill.textContent = state;
  pill.classList.toggle("live", state === "connecting");
}

/**
 * Open an event socket. `onEvent` is called with each event's flat payload.
 *
 * Two cursors are carried, because the server replays from two places: `cursor`
 * over the event rows, and `lines` over the stored transcript, which is not in
 * the database. A reconnect sends both back, so it resumes where it stopped
 * instead of restarting the transcript or losing the middle of it.
 *
 * Returns a handle with `close()` and the current cursors.
 */
export function connect(path, onEvent, { onStatus } = {}) {
  let cursor = 0;
  let lines = 0;
  let socket = null;
  let closed = false;
  let backoff = 500;

  const open = () => {
    if (closed) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const url = new URL(path, location.href);
    url.protocol = scheme + ":";
    url.searchParams.set("cursor", String(cursor));
    url.searchParams.set("lines", String(lines));
    socket = new WebSocket(url.toString());
    setConnection("connecting");

    socket.onopen = () => { backoff = 500; };

    socket.onmessage = (message) => {
      let frame;
      try { frame = JSON.parse(message.data); } catch { return; }
      if (frame.type === "ping") return;
      if (frame.type === "live") {
        cursor = Math.max(cursor, frame.cursor || 0);
        lines = Math.max(lines, frame.lines || 0);
        setConnection("live");
        onStatus?.("live");
        return;
      }
      if (frame.type === "dropped") {
        // The server could not keep up with this tab. Reconnecting from the
        // cursor refills the gap rather than leaving a silent hole.
        socket.close();
        return;
      }
      const events = frame.type === "batch" ? frame.events : [frame.event];
      for (const event of events || []) {
        if (!event) continue;
        // Log lines carry seq 0 — they are never replayed from the event table,
        // so they must not move the row cursor past a record still to come.
        cursor = Math.max(cursor, event.seq || 0);
        if (event.type === "log.line") lines += 1;
        onEvent(event);
      }
    };

    socket.onclose = () => {
      if (closed) return;
      setConnection("reconnecting");
      onStatus?.("reconnecting");
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 10000);
    };

    socket.onerror = () => socket?.close();
  };

  open();
  return {
    close() { closed = true; socket?.close(); },
    get cursor() { return cursor; },
    get lines() { return lines; },
  };
}
