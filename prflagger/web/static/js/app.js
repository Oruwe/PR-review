/* Chrome that every page shares: the theme toggle and the tooltip singleton. */

const root = document.documentElement;

function current() {
  const stamped = root.getAttribute("data-theme");
  if (stamped) return stamped;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

document.getElementById("theme")?.addEventListener("click", () => {
  const next = current() === "dark" ? "light" : "dark";
  root.setAttribute("data-theme", next);
  try { localStorage.setItem("pf-theme", next); } catch { /* private mode */ }
  // Charts read colour from CSS custom properties, so they need a repaint.
  window.dispatchEvent(new CustomEvent("pf:theme", { detail: next }));
});

/* One tooltip element, moved around. Charts call show/hide. */
const element = document.getElementById("tip");

export function showTip(html, event) {
  if (!element) return;
  element.innerHTML = html;
  element.style.opacity = "1";
  const box = element.getBoundingClientRect();
  const x = Math.min(event.clientX + 14, window.innerWidth - box.width - 10);
  const y = Math.max(10, event.clientY - box.height - 12);
  element.style.left = `${x}px`;
  element.style.top = `${y}px`;
}

export function hideTip() {
  if (element) element.style.opacity = "0";
}

window.pfTip = { showTip, hideTip };
