(() => {
    "use strict";

    const root = document.documentElement;
    const body = document.body;
    const clock = document.getElementById("liveClock");
    const refreshButton = document.getElementById("refreshButton");
    const fullscreenButton = document.getElementById("fullscreenButton");
    const themeButton = document.getElementById("themeToggle");
    const themeIcon = document.getElementById("themeToggleIcon");
    const themeText = document.getElementById("themeToggleText");
    const countdown = document.getElementById("refreshCountdown");
    const realtimeIndicator = document.getElementById("realtimeIndicator");
    const realtimeTransport = document.getElementById("realtimeTransport");
    const realtimeUrl = body?.dataset.realtimeUrl || "";
    const realtimeStreamUrl = body?.dataset.realtimeStreamUrl || "";
    const realtimePollMs = Math.max(500, Number(body?.dataset.realtimePollMs || 1000) || 1000);
    const detailMachineCode = body?.dataset.machineDetailCode || "";

    const refreshSeconds = Math.max(
        0,
        Number(body?.dataset.autoRefreshSeconds || 0) || 0
    );

    const pad = (value) => String(value).padStart(2, "0");
    const stateNames = ["run", "stop", "alarm", "offline", "unknown"];
    let baseDocumentTitle = document.title.replace(/^⚠\s*/, "");
    let fallbackTimer = null;
    let eventSource = null;

    function updateClock() {
        if (!clock) return;
        const now = new Date();
        clock.textContent =
            `${pad(now.getDate())}/${pad(now.getMonth() + 1)}/${now.getFullYear()} ` +
            `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
    }

    function syncThemeButton() {
        if (!themeButton) return;
        const isDark = root.getAttribute("data-theme") === "dark";
        if (themeIcon) themeIcon.textContent = isDark ? "☀" : "◐";
        if (themeText) themeText.textContent = isDark ? "Sáng" : "Tối";
        themeButton.setAttribute("aria-pressed", isDark ? "true" : "false");
    }

    function toggleTheme() {
        const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try {
            localStorage.setItem("machine-monitor-theme", next);
        } catch (_error) {}
        syncThemeButton();
        requestAnimationFrame(drawMachineChart);
    }

    const machineRows = new Map();
    document.querySelectorAll("[data-machine-code]").forEach((row) => {
        machineRows.set(row.dataset.machineCode, row);
    });

    function setText(id, value) {
        const node = document.getElementById(id);
        if (node) node.textContent = String(value);
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function setRealtimeStatus(label, transport) {
        if (realtimeIndicator) realtimeIndicator.textContent = label;
        if (realtimeTransport) realtimeTransport.textContent = transport;
    }

    function updateMachineRow(machine) {
        const row = machineRows.get(machine.code);
        if (!row) return;

        stateNames.forEach((state) => row.classList.remove(`pro-machine-${state}`));
        row.classList.add(`pro-machine-${machine.state}`);

        const badge = row.querySelector('[data-role="state-badge"]');
        if (badge) {
            stateNames.forEach((state) => badge.classList.remove(`pro-state-${state}`));
            badge.classList.add(`pro-state-${machine.state}`);
            badge.textContent = machine.state_label;
        }

        const production = row.querySelector('[data-role="production-count"]');
        if (production) production.textContent = machine.production_count ?? "—";

        const cycle = row.querySelector('[data-role="cycle-time"]');
        if (cycle) {
            cycle.textContent = machine.state === "offline"
                ? "—"
                : (machine.cycle_time_ms ?? "—");
        }

        const mode = row.querySelector('[data-role="mode"]');
        if (mode) mode.textContent = machine.auto_mode_bit ? "AUTO" : "MANUAL";

        const age = row.querySelector('[data-role="age"]');
        if (age) {
            const seconds = machine.age_seconds;
            if (seconds === null || seconds === undefined) {
                age.innerHTML = "<strong>—</strong>";
            } else {
                const suffix = machine.state === "offline" ? "chưa cập nhật" : "trước";
                age.innerHTML = `<strong>${Number(seconds)}s</strong><small>${suffix}</small>`;
            }
        }
    }

    function renderNotice(summary, machines) {
        const notice = document.getElementById("systemNotice");
        if (!notice) return;

        const alarm = Number(summary.alarm || 0);
        const offline = Number(summary.offline || 0);
        const unknown = Number(summary.unknown || 0);
        const firstAlarm = machines.find((item) => item.state === "alarm");
        const firstOffline = machines.find((item) => item.state === "offline");

        notice.className = "pro-notice";
        if (alarm > 0) {
            notice.classList.add("pro-notice-alarm");
            notice.innerHTML = `<span class="pro-notice-icon">!</span><div><strong>${alarm} máy đang phát sinh cảnh báo</strong><span>Trạng thái PLC vừa thay đổi. Ưu tiên kiểm tra ngay.</span></div><a href="${escapeHtml(firstAlarm?.detail_url || "/alarms/")}">Mở máy <b>→</b></a>`;
        } else if (offline > 0) {
            notice.classList.add("pro-notice-warning");
            notice.innerHTML = `<span class="pro-notice-icon">!</span><div><strong>${offline} máy đang mất dữ liệu</strong><span>Kiểm tra Collector hoặc kết nối PLC.</span></div><a href="${escapeHtml(firstOffline?.detail_url || "/system/")}">Chẩn đoán <b>→</b></a>`;
        } else if (unknown > 0) {
            notice.classList.add("pro-notice-warning");
            notice.innerHTML = `<span class="pro-notice-icon">?</span><div><strong>${unknown} máy có trạng thái chưa xác định</strong><span>Dữ liệu vẫn đến nhưng RUN/STOP chưa tạo trạng thái hợp lệ.</span></div><a href="/system/">Chẩn đoán <b>→</b></a>`;
        } else {
            notice.classList.add("pro-notice-ok");
            notice.innerHTML = `<span class="pro-notice-icon">✓</span><div><strong>Hệ thống đang cập nhật bình thường</strong><span>Không có Alarm hoạt động và dữ liệu PLC đang được push realtime.</span></div>`;
        }
    }

    function renderAttention(summary, machines) {
        const list = document.getElementById("attentionList");
        const count = document.getElementById("attentionCount");
        if (!list) return;

        const attention = machines.filter((item) =>
            ["alarm", "offline", "unknown"].includes(item.state)
        );
        if (count) count.textContent = String(attention.length);

        if (!attention.length) {
            list.innerHTML = '<div class="pro-attention-clear"><span>✓</span><strong>Không có sự cố cần xử lý</strong><small>Các máy đang được cập nhật bình thường.</small></div>';
            return;
        }

        list.innerHTML = attention.map((item) => {
            let cls = "attention-unknown";
            let icon = "?";
            let text = "Trạng thái chưa xác định";
            let small = "RUN/STOP chưa hợp lệ";
            if (item.state === "alarm") {
                cls = "attention-alarm";
                icon = "!";
                text = item.alarm_bit
                    ? `Alarm · Mã ${item.alarm_code ?? 0}`
                    : "RUN và STOP cùng ON";
                small = "Cần kiểm tra ngay";
            } else if (item.state === "offline") {
                cls = "attention-offline";
                icon = "○";
                text = "Mất dữ liệu";
                small = `Không cập nhật ${item.age_seconds ?? "—"} giây`;
            }
            return `<a class="pro-attention-item ${cls}" href="${escapeHtml(item.detail_url)}"><span class="pro-attention-icon">${icon}</span><div><strong>${escapeHtml(item.code)}</strong><span>${escapeHtml(text)}</span><small>${escapeHtml(small)}</small></div><b>›</b></a>`;
        }).join("");
    }

    function updateSignalLamp(id, value, activeClass) {
        const lamp = document.getElementById(`signalLamp${id}`);
        const text = document.getElementById(`signalValue${id}`);
        if (lamp) {
            lamp.classList.remove("lamp-on", "lamp-warning", "lamp-alarm", "lamp-info");
            if (value) lamp.classList.add(activeClass);
        }
        if (text) text.textContent = value ? "ON" : "OFF";
    }

    function updateMachineDetail(machine) {
        if (!detailMachineCode || machine.code !== detailMachineCode) return;

        const hero = document.getElementById("operation");
        const label = document.getElementById("machineStateLabel");
        const description = document.getElementById("machineStateDescription");
        if (hero) {
            stateNames.forEach((state) => hero.classList.remove(`state-border-${state}`));
            hero.classList.add(`state-border-${machine.state}`);
        }
        if (label) {
            stateNames.forEach((state) => label.classList.remove(`state-text-${state}`));
            label.classList.add(`state-text-${machine.state}`);
            label.textContent = machine.state_label;
        }
        if (description) {
            if (machine.state === "alarm") {
                description.textContent = machine.alarm_bit
                    ? `Máy đang phát sinh cảnh báo. Kiểm tra mã ${machine.alarm_code ?? 0}.`
                    : "RUN và STOP đang cùng ON. Kiểm tra logic/trạng thái tín hiệu.";
            } else if (machine.state === "offline") {
                description.textContent = "Dữ liệu PLC đang gián đoạn. Kiểm tra Collector hoặc Ethernet.";
            } else if (machine.state === "run") {
                description.textContent = "Máy đang thực hiện chu kỳ sản xuất.";
            } else if (machine.state === "stop") {
                description.textContent = "Máy đang ở trạng thái dừng.";
            } else {
                description.textContent = "RUN, STOP và ALARM đều chưa xác định rõ.";
            }
        }

        setText("machineProductionCount", machine.production_count ?? "—");
        setText("machineCycleTime", machine.cycle_time_ms ?? "—");
        setText("machineMode", machine.auto_mode_bit ? "AUTO" : "MANUAL");
        setText("machineSource", machine.source || "PLC");
        setText("machineConnectionLabel", machine.connection_label || "—");

        updateSignalLamp("Run", machine.run_bit, "lamp-on");
        updateSignalLamp("Stop", machine.stop_bit, "lamp-warning");
        updateSignalLamp("Alarm", machine.alarm_bit, "lamp-alarm");
        updateSignalLamp("Auto", machine.auto_mode_bit, "lamp-info");
    }

    function applyRealtimePayload(payload) {
        if (!payload?.ok) return;
        const summary = payload.summary || {};
        const machines = Array.isArray(payload.machines) ? payload.machines : [];

        setText("kpiTotal", summary.total ?? 0);
        setText("kpiRun", summary.run ?? 0);
        setText("kpiStop", summary.stop ?? 0);
        setText("kpiAlarm", summary.alarm ?? 0);
        setText("kpiOffline", summary.offline ?? 0);
        setText("machineTableCount", `${summary.total ?? 0} máy`);
        setText("collectorOfflineCount", summary.offline ?? 0);
        setText("collectorAlarmCount", summary.alarm ?? 0);
        setText("collectorHealthText", (summary.offline ?? 0) > 0 ? "Đang gián đoạn" : "Đang cập nhật");

        const totalNote = document.getElementById("kpiTotalNote");
        if (totalNote) {
            totalNote.textContent = (summary.unknown ?? 0) > 0
                ? `${summary.unknown} chưa xác định`
                : "máy đang cấu hình";
        }

        machines.forEach((machine) => {
            updateMachineRow(machine);
            updateMachineDetail(machine);
        });
        renderNotice(summary, machines);
        renderAttention(summary, machines);

        const alarmCount = Number(summary.alarm || 0);
        document.title = alarmCount > 0
            ? `⚠ ${baseDocumentTitle}`
            : baseDocumentTitle;
    }

    async function pollRealtimeState() {
        if (!realtimeUrl || document.hidden) return;
        try {
            const separator = realtimeUrl.includes("?") ? "&" : "?";
            const response = await fetch(`${realtimeUrl}${separator}_=${Date.now()}`, {
                method: "GET",
                cache: "no-store",
                headers: {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            applyRealtimePayload(await response.json());
        } catch (error) {
            console.warn("Realtime fallback polling thất bại:", error);
        }
    }

    function startFallbackPolling() {
        if (!realtimeUrl || fallbackTimer) return;
        setRealtimeStatus("FALLBACK", "1s POLL");
        pollRealtimeState();
        fallbackTimer = window.setInterval(pollRealtimeState, realtimePollMs);
    }

    function stopFallbackPolling() {
        if (!fallbackTimer) return;
        window.clearInterval(fallbackTimer);
        fallbackTimer = null;
    }

    function startRealtimeStream() {
        if (!realtimeStreamUrl || !("EventSource" in window)) {
            startFallbackPolling();
            return Boolean(realtimeUrl);
        }

        setRealtimeStatus("ĐANG KẾT NỐI", "SSE");
        eventSource = new EventSource(realtimeStreamUrl);

        eventSource.addEventListener("open", () => {
            stopFallbackPolling();
            setRealtimeStatus("LIVE", "SSE PUSH");
        });

        eventSource.addEventListener("state", (event) => {
            try {
                applyRealtimePayload(JSON.parse(event.data));
                setRealtimeStatus("LIVE", "SSE PUSH");
            } catch (error) {
                console.error("SSE payload không hợp lệ:", error);
            }
        });

        eventSource.addEventListener("stream_error", (event) => {
            console.warn("Realtime stream backend warning:", event.data);
            startFallbackPolling();
        });

        eventSource.onerror = () => {
            // EventSource tự reconnect; trong lúc đó dùng JSON polling 1 giây.
            setRealtimeStatus("RECONNECT", "FALLBACK");
            startFallbackPolling();
        };

        window.addEventListener("beforeunload", () => eventSource?.close(), {once: true});
        return true;
    }

    function startAutoRefresh() {
        if (refreshSeconds <= 0) return;
        let remaining = refreshSeconds;
        window.setInterval(() => {
            if (document.hidden) {
                remaining = refreshSeconds;
                if (countdown) countdown.textContent = String(refreshSeconds);
                return;
            }
            remaining -= 1;
            if (countdown) countdown.textContent = String(Math.max(remaining, 0));
            if (remaining <= 0) window.location.reload();
        }, 1000);
    }

    function readChartData(nodeId, fieldName) {
        const dataNode = document.getElementById(nodeId);
        if (!dataNode) return [];

        try {
            const rows = JSON.parse(dataNode.textContent || "[]");
            if (!Array.isArray(rows)) return [];

            return rows.filter((row) => {
                const value = row?.[fieldName];
                return value !== null && value !== undefined && value !== "" &&
                    Number.isFinite(Number(value));
            });
        } catch (error) {
            console.error("Không đọc được dữ liệu biểu đồ:", error);
            return [];
        }
    }

    function drawLineChart({
        canvasId,
        dataNodeId,
        valueField,
        unit = "",
        height = 310,
    }) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const rows = readChartData(dataNodeId, valueField);
        const parent = canvas.parentElement;
        const width = Math.max((parent?.clientWidth || 800) - 40, 300);
        const dpr = Math.max(1, window.devicePixelRatio || 1);

        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;

        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        ctx.font = '11px "Segoe UI", sans-serif';

        if (!rows.length) {
            ctx.fillStyle = "#8295a5";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText("Chưa có dữ liệu để hiển thị", width / 2, height / 2);
            return;
        }

        const padding = { left: 70, right: 20, top: 25, bottom: 45 };
        const chartWidth = width - padding.left - padding.right;
        const chartHeight = height - padding.top - padding.bottom;
        const values = rows.map((row) => Number(row[valueField]));

        let min = Math.min(...values);
        let max = Math.max(...values);

        if (min === max) {
            const margin = Math.max(Math.abs(min) * 0.05, 100);
            min -= margin;
            max += margin;
        } else {
            const margin = (max - min) * 0.15;
            min -= margin;
            max += margin;
        }

        ctx.textAlign = "right";
        ctx.textBaseline = "middle";

        for (let index = 0; index <= 5; index += 1) {
            const ratio = index / 5;
            const y = padding.top + ratio * chartHeight;
            const value = max - ratio * (max - min);

            ctx.strokeStyle = "rgba(128, 170, 190, 0.18)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(padding.left, y);
            ctx.lineTo(width - padding.right, y);
            ctx.stroke();

            ctx.fillStyle = "#82a8bb";
            ctx.fillText(
                `${Math.round(value)}${unit ? ` ${unit}` : ""}`,
                padding.left - 10,
                y
            );
        }

        const points = rows.map((row, index) => {
            const x = padding.left +
                (index / Math.max(rows.length - 1, 1)) * chartWidth;
            const value = Number(row[valueField]);
            const y = padding.top + ((max - value) / (max - min)) * chartHeight;
            return { x, y, row };
        });

        if (points.length > 1) {
            const gradient = ctx.createLinearGradient(
                0,
                padding.top,
                0,
                height - padding.bottom
            );
            gradient.addColorStop(0, "rgba(77, 180, 232, 0.30)");
            gradient.addColorStop(1, "rgba(77, 180, 232, 0.02)");

            ctx.beginPath();
            ctx.moveTo(points[0].x, height - padding.bottom);
            points.forEach((point) => ctx.lineTo(point.x, point.y));
            ctx.lineTo(points[points.length - 1].x, height - padding.bottom);
            ctx.closePath();
            ctx.fillStyle = gradient;
            ctx.fill();
        }

        ctx.strokeStyle = "#51d7f3";
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        points.forEach((point, index) => {
            if (index === 0) ctx.moveTo(point.x, point.y);
            else ctx.lineTo(point.x, point.y);
        });
        ctx.stroke();

        ctx.fillStyle = "#72e6ff";
        points.forEach((point) => {
            ctx.beginPath();
            ctx.arc(point.x, point.y, 3, 0, Math.PI * 2);
            ctx.fill();
        });

        ctx.fillStyle = "#82a8bb";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        const labelCount = Math.min(6, rows.length);
        for (let index = 0; index < labelCount; index += 1) {
            const rowIndex = Math.round(
                (index / Math.max(labelCount - 1, 1)) * (rows.length - 1)
            );
            const point = points[rowIndex];

            if (point?.row?.time) {
                ctx.fillText(point.row.time, point.x, height - 18);
            }
        }
    }

    function drawMachineChart() {
        drawLineChart({
            canvasId: "cycleChart",
            dataNodeId: "machine-chart-data",
            valueField: "cycle",
            unit: "ms",
            height: 310,
        });
    }

    let resizeTimer = null;

    updateClock();
    window.setInterval(updateClock, 1000);

    refreshButton?.addEventListener("click", () => window.location.reload());
    themeButton?.addEventListener("click", toggleTheme);

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

    window.addEventListener("resize", () => {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(
            () => window.requestAnimationFrame(drawMachineChart),
            120
        );
    });

    syncThemeButton();
    drawMachineChart();
    if (!startRealtimeStream()) {
        startAutoRefresh();
    }
})();
