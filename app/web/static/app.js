(() => {
  const list = document.getElementById("automation-list");
  if (!list) return;

  const runningEl = document.querySelector("[data-running]");
  const totalEl = document.querySelector("[data-total]");

  function updateStats() {
    const items = [...list.querySelectorAll(".automation")];
    const running = items.filter((el) => el.dataset.status === "running").length;
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

    const startBtn = row.querySelector('[data-action="start"]');
    const stopBtn = row.querySelector('[data-action="stop"]');
    if (startBtn) {
      startBtn.disabled = ["running", "starting"].includes(payload.status);
    }
    if (stopBtn) {
      stopBtn.disabled = !["running", "error"].includes(payload.status);
    }

    updateStats();
  }

  async function toggle(row, action) {
    const id = row.dataset.id;
    const buttons = row.querySelectorAll("button");
    buttons.forEach((btn) => {
      btn.disabled = true;
    });

    row.dataset.status = action === "start" ? "starting" : "stopping";
    const label = row.querySelector("[data-status-label]");
    if (label) label.textContent = row.dataset.status;

    try {
      const response = await fetch(`/api/automations/${id}/${action}`, {
        method: "POST",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        applyState(row, {
          status: "error",
          error: payload.detail || `Failed to ${action}`,
        });
        return;
      }
      applyState(row, payload);
    } catch (err) {
      applyState(row, {
        status: "error",
        error: err?.message || `Failed to ${action}`,
      });
    }
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const row = button.closest(".automation");
    if (!row) return;
    toggle(row, button.dataset.action);
  });
})();
