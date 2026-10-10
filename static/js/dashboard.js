/**
 * dashboard.js
 * --------------
 * Handles Chart.js chart rendering for the dashboard and analytics pages,
 * plus the polling logic used by video_status.html to check processing
 * progress without a manual page refresh.
 */

// Global Chart.js dark-theme defaults so every chart matches the ops-center look
if (typeof Chart !== "undefined") {
  Chart.defaults.color = "#8b96ab";
  Chart.defaults.font.family = "'Inter', 'Segoe UI', sans-serif";
  Chart.defaults.borderColor = "rgba(255,255,255,0.08)";
}

/**
 * Renders the person-count line chart on the analytics page.
 * @param {string} canvasId - id of the <canvas> element
 * @param {number[]} timestamps - x-axis values (seconds)
 * @param {number[]} counts - y-axis values (person count)
 */
function renderPersonCountChart(canvasId, timestamps, counts) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  new Chart(ctx, {
    type: "line",
    data: {
      labels: timestamps.map((t) => `${t.toFixed(1)}s`),
      datasets: [{
        label: "People Count",
        data: counts,
        borderColor: "#38bdf8",
        backgroundColor: "rgba(56, 189, 248, 0.12)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: "rgba(255,255,255,0.05)" } },
        y: { beginAtZero: true, grid: { color: "rgba(255,255,255,0.05)" } },
      },
    },
  });
}

/**
 * Renders the risk-score timeline chart on the analytics page.
 */
function renderRiskChart(canvasId, timestamps, riskScores) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  new Chart(ctx, {
    type: "line",
    data: {
      labels: timestamps.map((t) => `${t.toFixed(1)}s`),
      datasets: [{
        label: "Risk Score",
        data: riskScores,
        borderColor: "#ef4444",
        backgroundColor: "rgba(239, 68, 68, 0.12)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: "rgba(255,255,255,0.05)" } },
        y: { min: 0, max: 1, grid: { color: "rgba(255,255,255,0.05)" } },
      },
    },
  });
}

/**
 * Polls /api/video/<id>/status every 3 seconds until processing is no
 * longer "pending" or "processing", then reloads the page to show results.
 */
function pollVideoStatus(videoId) {
  const statusLabel = document.getElementById("processing-status-label");

  const interval = setInterval(async () => {
    try {
      const response = await fetch(`/api/video/${videoId}/status`);
      const data = await response.json();

      if (statusLabel) {
        statusLabel.textContent = data.processing_status;
      }

      if (data.processing_status === "completed" || data.processing_status === "failed") {
        clearInterval(interval);
        window.location.reload();
      }
    } catch (err) {
      console.error("Status poll failed:", err);
    }
  }, 3000);
}

/**
 * Polls /api/live/status every 1.5 seconds while a live CCTV session is
 * running and updates the stat cards on live_view.html (person count,
 * density, risk level/badge color, FPS, Fluvio vs direct-mode badge).
 * Stops polling automatically if the session is no longer running.
 */
function pollLiveStatus() {
  const countEl = document.getElementById("live-person-count");
  const densityEl = document.getElementById("live-density");
  const riskBadgeEl = document.getElementById("live-risk-badge");
  const fpsEl = document.getElementById("live-fps");
  const modeEl = document.getElementById("live-mode-badge");

  const interval = setInterval(async () => {
    try {
      const response = await fetch("/api/live/status");
      const data = await response.json();

      if (!data.is_running) {
        clearInterval(interval);
        return;
      }

      if (countEl) countEl.textContent = data.person_count;
      if (densityEl) densityEl.textContent = data.max_cell_density;
      if (fpsEl) fpsEl.textContent = data.fps.toFixed(1);

      if (riskBadgeEl) {
        riskBadgeEl.textContent = data.risk_label;
        riskBadgeEl.style.backgroundColor = data.risk_color;
      }

      if (modeEl) {
        modeEl.textContent = data.fluvio_active ? "FLUVIO STREAMING" : "DIRECT MODE";
        modeEl.className = data.fluvio_active
          ? "badge bg-success"
          : "badge bg-warning text-dark";
      }
    } catch (err) {
      console.error("Live status poll failed:", err);
    }
  }, 1500);
}

// Dark-mode toggle: persists preference in-memory only (no localStorage in this environment)
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("dark-mode-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      document.body.classList.toggle("light-mode");
    });
  }
});
