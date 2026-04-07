/**
 * Preload script — runs in the renderer process before the page loads.
 *
 * Context isolation is enabled, so this script cannot directly access the
 * page's JavaScript context. It handles three concerns:
 *
 * 1. Block right-click context menu at the renderer level (belt-and-suspenders
 *    with the main process context-menu handler).
 * 2. Block middle-click auto-scroll, which conflicts with RTS panning.
 * 3. Block drag-and-drop navigation (dropping a file onto the window would
 *    navigate away from the app).
 */

window.addEventListener("contextmenu", (e) => {
  e.preventDefault();
});

window.addEventListener("auxclick", (e) => {
  // Middle-click (button 1) — prevent browser auto-scroll
  if (e.button === 1) {
    e.preventDefault();
  }
});

window.addEventListener("dragover", (e) => {
  e.preventDefault();
});

window.addEventListener("drop", (e) => {
  e.preventDefault();
});

// Block Backspace from navigating back (some Chromium builds still do this)
window.addEventListener("keydown", (e) => {
  if (e.key === "Backspace" && e.target === document.body) {
    e.preventDefault();
  }
});
