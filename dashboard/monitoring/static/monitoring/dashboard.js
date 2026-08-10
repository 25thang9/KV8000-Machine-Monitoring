(() => {
  "use strict";

  const clock = document.getElementById("liveClock");
  const refreshButton = document.getElementById("refreshButton");
  const fullscreenButton = document.getElementById("fullscreenButton");
  const countdown = document.getElementById("refreshCountdown");
  const refreshSeconds = Number(document.body.dataset.autoRefreshSeconds || 0);

  const pad = (value) => String(value).padStart(2, "0");

  function updateClock() {
    if (!clock) return;
    const now = new Date();
    clock.textContent = `${pad(now.getDate())}/${pad(now.getMonth() + 1)}/${now.getFullYear()} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }

  updateClock();
  window.setInterval(updateClock, 1000);

  refreshButton?.addEventListener("click", () => window.location.reload());

  fullscreenButton?.addEventListener("click", async () => {
    try {
      if (!document.fullscreenElement) {
        await document.documentElement.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch (error) {
      console.error("Không thể đổi chế độ toàn màn hình:", error);
    }
  });

  if (refreshSeconds > 0) {
    let remaining = refreshSeconds;
    window.setInterval(() => {
      remaining -= 1;
      if (countdown) countdown.textContent = String(Math.max(remaining, 0));
      if (remaining <= 0) window.location.reload();
    }, 1000);
  }

  function drawCycleChart() {
    const canvas = document.getElementById("cycleChart");
    const dataNode = document.getElementById("machine-chart-data");
    if (!canvas || !dataNode) return;

    let rows;
    try {
      rows = JSON.parse(dataNode.textContent || "[]").filter(
        (row) => Number.isFinite(Number(row.cycle))
      );
    } catch (error) {
      console.error("Không đọc được dữ liệu biểu đồ:", error);
      return;
    }

    if (!rows.length) return;

    const parentWidth = canvas.parentElement?.clientWidth || 800;
    const cssHeight = 310;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = parentWidth * dpr;
    canvas.height = cssHeight * dpr;
    canvas.style.width = `${parentWidth}px`;
    canvas.style.height = `${cssHeight}px`;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const width = parentWidth;
    const height = cssHeight;
    const padding = { left: 58, right: 18, top: 20, bottom: 40 };
    const values = rows.map((row) => Number(row.cycle));
    let min = Math.min(...values);
    let max = Math.max(...values);
    const spread = Math.max(1, max - min);
    min -= spread * 0.15;
    max += spread * 0.15;

    ctx.clearRect(0, 0, width, height);
    ctx.font = '11px "Segoe UI", sans-serif';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;

    for (let index = 0; index <= 5; index += 1) {
      const ratio = index / 5;
      const y = padding.top + ratio * chartHeight;
      const value = max - ratio * (max - min);
      ctx.strokeStyle = "rgba(128, 150, 168, 0.18)";
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(width - padding.right, y);
      ctx.stroke();
      ctx.fillStyle = "#8295a5";
      ctx.fillText(`${Math.round(value)} ms`, padding.left - 10, y);
    }

    const points = rows.map((row, index) => {
      const x = padding.left + (index / Math.max(1, rows.length - 1)) * chartWidth;
      const y = padding.top + ((max - Number(row.cycle)) / (max - min)) * chartHeight;
      return { x, y, row };
    });

    ctx.strokeStyle = "#4db4e8";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((point, index) => {
      if (index === 0) ctx.moveTo(point.x, point.y);
      else ctx.lineTo(point.x, point.y);
    });
    ctx.stroke();

    ctx.fillStyle = "#4db4e8";
    points.forEach((point) => {
      ctx.beginPath();
      ctx.arc(point.x, point.y, 2.5, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.fillStyle = "#8295a5";
    ctx.textAlign = "center";
    const labels = Math.min(6, rows.length);
    for (let index = 0; index < labels; index += 1) {
      const rowIndex = Math.round((index / Math.max(1, labels - 1)) * (rows.length - 1));
      const point = points[rowIndex];
      ctx.fillText(point.row.time, point.x, height - 16);
    }
  }

  drawCycleChart();
  window.addEventListener("resize", () => window.requestAnimationFrame(drawCycleChart));
})();
