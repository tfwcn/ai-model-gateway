/**
 * 折线图与柱状图：只依赖 canvas，不引入任何图表库
 *
 * 图表只吃「已排序的桶 + 数值」，视图层负责把接口数据整理成这个形状。
 */

const PADDING = { top: 14, right: 12, bottom: 22, left: 48 };

function setup(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = canvas.clientHeight || 200;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);

  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.font = "11px system-ui, sans-serif";
  return { ctx, width, height };
}

// 注意：getComputedStyle 只接受元素，传 canvas 的 2d 上下文会抛 TypeError
function colors(canvas) {
  const style = getComputedStyle(canvas);
  return {
    line: style.getPropertyValue("--line") || "#d8dce3",
    ink: style.getPropertyValue("--muted") || "#6b7280",
    accent: style.getPropertyValue("--accent") || "#2f6feb",
  };
}

function axis(ctx, box, palette, labels, maxValue) {
  const plotW = box.width - PADDING.left - PADDING.right;
  const plotH = box.height - PADDING.top - PADDING.bottom;

  ctx.strokeStyle = palette.line;
  ctx.fillStyle = palette.ink;
  ctx.lineWidth = 1;

  // 横向网格 + 刻度
  for (let step = 0; step <= 4; step += 1) {
    const value = (maxValue * step) / 4;
    const y = PADDING.top + plotH - (plotH * step) / 4;
    ctx.beginPath();
    ctx.moveTo(PADDING.left, y);
    ctx.lineTo(PADDING.left + plotW, y);
    ctx.stroke();
    ctx.fillText(shortNumber(value), 6, y + 4);
  }

  // 稀疏的桶标签，避免挤成一团
  const stride = Math.max(1, Math.ceil(labels.length / 8));
  labels.forEach((label, index) => {
    if (index % stride !== 0) return;
    const x = PADDING.left + (plotW * (index + 0.5)) / labels.length;
    ctx.fillText(label, x - 18, box.height - 6);
  });
}

function shortNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
  return String(Math.round(num));
}

/**
 * 柱状图：每个桶一根柱子
 *
 * series: [{label, points:[{bucket, value}]}]
 */
export function barChart(canvas, labels, values) {
  const box = setup(canvas);
  const palette = colors(canvas);
  const plotW = box.width - PADDING.left - PADDING.right;
  const plotH = box.height - PADDING.top - PADDING.bottom;
  const maxValue = Math.max(1, ...values.map((value) => Number(value) || 0));

  axis(box.ctx, box, palette, labels, maxValue);

  const slot = plotW / Math.max(1, values.length);
  const barWidth = Math.max(2, slot * 0.6);
  box.ctx.fillStyle = palette.accent;

  values.forEach((value, index) => {
    const num = Number(value) || 0;
    const height = (plotH * num) / maxValue;
    const x = PADDING.left + slot * index + (slot - barWidth) / 2;
    box.ctx.fillRect(x, PADDING.top + plotH - height, barWidth, height);
  });
}

/**
 * 折线图：可以叠加多条线（例如请求数与 P95 耗时）
 *
 * series: [{label, values:[...], color?}]
 */
export function lineChart(canvas, labels, series) {
  const box = setup(canvas);
  const palette = colors(canvas);
  const plotW = box.width - PADDING.left - PADDING.right;
  const plotH = box.height - PADDING.top - PADDING.bottom;

  const all = series.flatMap((item) => item.values.map((value) => Number(value) || 0));
  const maxValue = Math.max(1, ...all);
  axis(box.ctx, box, palette, labels, maxValue);

  series.forEach((item, order) => {
    const stroke = item.color || (order === 0 ? palette.accent : palette.ink);
    box.ctx.strokeStyle = stroke;
    box.ctx.fillStyle = stroke;
    box.ctx.lineWidth = 2;

    // 缺失的桶直接断开：先把已有的一段画完，下一个有效点重新起笔，不连成假趋势
    let started = false;
    let points = 0;
    item.values.forEach((value, index) => {
      const num = Number(value);
      if (!Number.isFinite(num)) {
        if (started) box.ctx.stroke();
        started = false;
        return;
      }
      const x = PADDING.left + (plotW * (index + 0.5)) / item.values.length;
      const y = PADDING.top + plotH - (plotH * num) / maxValue;
      if (!started) {
        box.ctx.beginPath();
        box.ctx.moveTo(x, y);
        started = true;
        points = 1;
        return;
      }
      box.ctx.lineTo(x, y);
      points += 1;
    });
    if (!started) return;
    box.ctx.stroke();
    // 只有一个点的段画不出线，补一个小圆点，否则图上什么都看不见
    if (points === 1) box.ctx.fill();
  });

  legend(box.ctx, box, palette, series);
}

function legend(ctx, box, palette, series) {
  let x = PADDING.left;
  series.forEach((item, order) => {
    ctx.fillStyle = item.color || (order === 0 ? palette.accent : palette.ink);
    ctx.fillRect(x, 4, 8, 8);
    ctx.fillStyle = palette.ink;
    ctx.fillText(item.label, x + 12, 12);
    x += 12 + ctx.measureText(item.label).width + 18;
  });
}
