/* Charts helper — thin wrapper around Chart.js with dark theme defaults */
const Charts = {
  instances: {},

  make(canvasId, type, labels, datasets, opts = {}) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    const cfg = {
      type,
      data: { labels, datasets },
      options: Object.assign({
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { labels: { color: "#8b94a8", boxWidth: 12, font: { size: 11 } } } },
        scales: {
          x: { ticks: { color: "#5a6273", font: { size: 10 }, maxTicksLimit: 8 }, grid: { color: "rgba(35,42,58,.5)" } },
          y: { ticks: { color: "#5a6273", font: { size: 10 } }, grid: { color: "rgba(35,42,58,.5)" } },
        },
      }, opts),
    };
    if (this.instances[canvasId]) { this.instances[canvasId].destroy(); }
    try {
      this.instances[canvasId] = new Chart(ctx, cfg);
    } catch (e) {
      console.error("Failed to create chart: " + canvasId, e);
      this.instances[canvasId] = null;
    }
    return this.instances[canvasId];
  },

  destroy(id) {
    if (this.instances[id]) { this.instances[id].destroy(); delete this.instances[id]; }
  },

  destroyAll(prefix) {
    Object.keys(this.instances).forEach((k) => {
      if (k.startsWith(prefix)) this.destroy(k);
    });
  },
};

/* Time-series chart fed from history points {ts, data} */
function seriesChart(id, label, getValue, color) {
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  const labels = [], values = [];
  const chart = Charts.make(id, "line", labels, [{
    label,
    data: values,
    borderColor: color || "#4f8cff",
    backgroundColor: (color || "#4f8cff") + "33",
    fill: true,
    tension: 0.25,
    pointRadius: 0,
    borderWidth: 1.5,
  }], {
    scales: {
      x: { ticks: { color: "#5a6273", font: { size: 10 }, maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
      y: { ticks: { color: "#5a6273", font: { size: 10 } }, grid: { color: "rgba(35,42,58,.5)" } },
    },
  });
  if (!chart) return null;
  return {
    chart,
    push(ts, v) {
      if (chart.data.labels.length > 600) { chart.data.labels.shift(); chart.data.datasets[0].data.shift(); }
      chart.data.labels.push(new Date(ts * 1000).toLocaleTimeString());
      chart.data.datasets[0].data.push(v);
      try { chart.update("none"); } catch (e) {}
    },
    load(points, getter) {
      chart.data.labels.length = 0;
      chart.data.datasets[0].data.length = 0;
      points.forEach((p) => {
        chart.data.labels.push(new Date(p.ts * 1000).toLocaleTimeString());
        chart.data.datasets[0].data.push(getter(p));
      });
      try { chart.update("none"); } catch (e) {}
    },
  };
}

/* Sparkline for GPU utilization bar blocks */
function renderUtilBar(container, pct) {
  const color = gpuPctColor(pct);
  const safe = Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
  const blocks = 10;
  const filled = Math.round((safe / 100) * blocks);
  let html = '<div class="gpu-bar"><span style="font-size:11px;color:var(--text-dim)">';
  for (let i = 0; i < blocks; i++) {
    html += `<span style="color:${i < filled ? color : "#2c3446"}">█</span>`;
  }
  html += `</span><span class="pct" style="color:${color}">${Number.isFinite(pct) ? pct.toFixed(0) + "%" : "—"}</span></div>`;
  container.innerHTML = html;
}
