/* Talking to the service: fetch helpers and the event socket.
 *
 * `connect` reconnects with the cursor it last saw, so a dropped socket resumes
 * the transcript rather than restarting it or losing the middle.
 */

export async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const text = await response.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!response.ok) {
    throw new Error((parsed && (parsed.detail || parsed.error)) || text || response.statusText);
  }
  return parsed;
}

export async function get(path) {
  const response = await fetch(path);
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
 * Returns a handle with `close()` and the current cursor.
 */
export function connect(path, onEvent, { onStatus } = {}) {
  let cursor = 0;
  let socket = null;
  let closed = false;
  let backoff = 500;

  const open = () => {
    if (closed) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const url = new URL(path, location.href);
    url.protocol = scheme + ":";
    url.searchParams.set("cursor", String(cursor));
    socket = new WebSocket(url.toString());
    setConnection("connecting");

    socket.onopen = () => { backoff = 500; };

    socket.onmessage = (message) => {
      let frame;
      try { frame = JSON.parse(message.data); } catch { return; }
      if (frame.type === "ping") return;
      if (frame.type === "live") {
        cursor = Math.max(cursor, frame.cursor || 0);
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
        cursor = Math.max(cursor, event.seq || 0);
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
  };
}
