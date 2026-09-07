(() => {
  const list = document.getElementById("automation-list");
  if (!list) return;

  const runningEl = document.querySelector("[data-running]");
  const totalEl = document.querySelector("[data-total]");

  function updateStats(items) {
    const running = items.filter((item) => item.status === "running").length;
    if (runningEl) runningEl.textContent = String(running);
    if (totalEl) totalEl.textContent = String(items.length);
  }

  function applyState(row, payload) {
    row.dataset.status = payload.status;

    const label = row.querySelector("[data-status-label]");
    if (label) label.textContent = payload.status;

    const error = row.querySelector("[data-error]");
    if (error) {
      if (payload.error) {
        error.textContent = payload.error;
        error.classList.remove("is-hidden");
      } else {
        error.textContent = "";
        error.classList.add("is-hidden");
      }
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/automations");
      if (!response.ok) return;
      const items = await response.json();
      if (!Array.isArray(items)) return;

      for (const payload of items) {
        const row = list.querySelector(`.automation[data-id="${payload.id}"]`);
        if (row) applyState(row, payload);
      }
      updateStats(items);
    } catch {
      // Keep last known UI state if refresh fails.
    }
  }

  refresh();
  setInterval(refresh, 5000);
})();
