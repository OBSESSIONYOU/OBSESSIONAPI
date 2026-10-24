import { shortNum } from "./dom.js";

function canvasSize(canvas, height = 250) {
  const width = Math.max(280, canvas.clientWidth || canvas.parentElement?.clientWidth || 600);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr; canvas.height = height * dpr;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height };
}

const withAlpha = (color, alpha) => {
  const value = String(color || "").trim();
  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (!hex) return value || `rgba(39,131,222,${alpha})`;
  let digits = hex[1];
  if (digits.length === 3) digits = digits.split("").map((d) => d + d).join("");
  const int = parseInt(digits, 16);
  return `rgba(${(int >> 16) & 255},${(int >> 8) & 255},${int & 255},${alpha})`;
};

function roundTopRect(ctx, x, y, width, height, radius) {
  const r = Math.max(0, Math.min(radius, width / 2, height));
  ctx.beginPath();
  ctx.moveTo(x, y + height);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height);
  ctx.closePath();
}

// Catmull-Rom → cubic bezier, with control points clamped inside the plot band.
function smoothPath(ctx, coords, minY, maxY) {
  const clamp = (value) => Math.max(minY, Math.min(maxY, value));
  for (let index = 0; index < coords.length - 1; index += 1) {
    const p0 = coords[index - 1] || coords[index];
    const p1 = coords[index];
    const p2 = coords[index + 1];
    const p3 = coords[index + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = clamp(p1[1] + (p2[1] - p0[1]) / 6);
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = clamp(p2[1] - (p3[1] - p1[1]) / 6);
    ctx.bezierCurveTo(c1x, c1y, c2x, c2y, p2[0], p2[1]);
  }
}

export function drawCombo(canvas, points, mode = "both") {
  const { ctx, width, height } = canvasSize(canvas);
  const styles = getComputedStyle(document.documentElement);
  const text = styles.getPropertyValue("--muted").trim() || "#55606E";
  const line = styles.getPropertyValue("--line").trim() || "#E1E6EE";
  const panel = styles.getPropertyValue("--panel").trim() || "#FFFFFF";
  const accent = styles.getPropertyValue("--accent").trim() || "#1668C4";
  const success = styles.getPropertyValue("--success").trim() || "#2C8A5E";
  const left = 44, right = 46, top = 18, bottom = 38;
  const innerW = width - left - right, innerH = height - top - bottom;

  const requests = points.map((point) => Number(point.requests || 0));
  const tokens = points.map((point) => Number(point.tokens || 0));
  const requestMax = Math.max(...requests, 1);
  const tokenMax = Math.max(...tokens, 1);
  const primaryMax = mode === "tokens" ? tokenMax : requestMax;
  const step = innerW / Math.max(points.length, 1);

  ctx.font = "12px 'IBM Plex Mono', Consolas, monospace";
  ctx.textBaseline = "middle";

  // grid — dashed horizontals
  ctx.setLineDash([4, 5]);
  ctx.strokeStyle = line; ctx.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = Math.round(top + innerH * index / 3) + .5;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - right, y); ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.fillStyle = text; ctx.textAlign = "right";
  for (let index = 0; index < 4; index += 1) {
    const y = top + innerH * index / 3;
    ctx.fillText(shortNum(primaryMax * (1 - index / 3)), left - 10, y);
  }
  if (mode === "both") {
    ctx.textAlign = "left";
    for (let index = 0; index < 4; index += 1) {
      const y = top + innerH * index / 3;
      ctx.fillText(shortNum(tokenMax * (1 - index / 3)), width - right + 10, y);
    }
  }

  // bars — client requests
  if (mode !== "tokens") {
    const barW = Math.max(3, Math.min(30, step * .5));
    const gradient = ctx.createLinearGradient(0, top, 0, top + innerH);
    gradient.addColorStop(0, withAlpha(accent, .95));
    gradient.addColorStop(1, withAlpha(accent, .34));
    ctx.fillStyle = gradient;
    requests.forEach((value, index) => {
      const x = left + step * index + (step - barW) / 2;
      const barH = value / requestMax * innerH;
      if (barH <= 0) return;
      roundTopRect(ctx, x, top + innerH - barH, barW, barH, Math.min(4, barW / 2));
      ctx.fill();
    });
  }

  // line + area — tokens (smoothed)
  if (mode !== "requests" && tokens.length) {
    const xAt = (index) => left + step * index + step / 2;
    const yAt = (index) => top + innerH - tokens[index] / tokenMax * innerH;
    const coords = tokens.map((_, index) => [xAt(index), yAt(index)]);

    const area = ctx.createLinearGradient(0, top, 0, top + innerH);
    area.addColorStop(0, withAlpha(success, .26));
    area.addColorStop(1, withAlpha(success, 0));
    ctx.beginPath();
    ctx.moveTo(coords[0][0], top + innerH);
    ctx.lineTo(coords[0][0], coords[0][1]);
    smoothPath(ctx, coords, top, top + innerH);
    ctx.lineTo(coords[coords.length - 1][0], top + innerH);
    ctx.closePath(); ctx.fillStyle = area; ctx.fill();

    ctx.strokeStyle = success; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(coords[0][0], coords[0][1]);
    smoothPath(ctx, coords, top, top + innerH);
    ctx.stroke();

    if (points.length <= 40) {
      coords.forEach(([x, y]) => {
        ctx.beginPath(); ctx.arc(x, y, 4.4, 0, Math.PI * 2); ctx.fillStyle = withAlpha(success, .16); ctx.fill();
        ctx.beginPath(); ctx.arc(x, y, 2.6, 0, Math.PI * 2); ctx.fillStyle = panel; ctx.fill();
        ctx.strokeStyle = success; ctx.lineWidth = 1.6; ctx.stroke();
      });
    }
  }

  // date axis
  const labelStep = Math.max(1, Math.ceil(points.length / Math.max(3, Math.floor(innerW / 64))));
  ctx.fillStyle = text; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
  points.forEach((point, index) => {
    if (index % labelStep !== 0) return;
    ctx.fillText(String(point.date || "").slice(5), left + step * index + step / 2, height - 12);
  });

  canvas._chartPoints = points;
  canvas._chartMeta = { left, top, innerW, innerH, step, max: primaryMax, mode };
}

export function drawBars(canvas, points) { drawCombo(canvas, points, "requests"); }

export function bindTooltip(canvas, tooltip, formatter = (point) => point) {
  canvas.onmousemove = (event) => {
    const meta = canvas._chartMeta; if (!meta || !canvas._chartPoints?.length) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const index = Math.max(0, Math.min(canvas._chartPoints.length - 1, Math.round((x - meta.left - meta.step / 2) / meta.step)));
    const point = canvas._chartPoints[index];
    tooltip.innerHTML = formatter(point);
    tooltip.classList.add("visible"); tooltip.style.left = `${Math.max(70, Math.min(rect.width - 20, x))}px`; tooltip.style.top = `${event.clientY - rect.top}px`;
  };
  canvas.onmouseleave = () => tooltip.classList.remove("visible");
}

export function observeCanvas(canvas, draw) {
  const resize = () => draw();
  const observer = new ResizeObserver(resize);
  if (canvas.parentElement) observer.observe(canvas.parentElement);
  draw();
  return () => { observer.disconnect(); canvas.onmousemove = null; canvas.onmouseleave = null; };
}
