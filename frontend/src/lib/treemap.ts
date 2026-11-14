export interface TreemapItem {
  key: string;
  value: number;
}

export interface TreemapRect {
  key: string;
  value: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

function worst(row: number[], length: number): number {
  const sum = row.reduce((a, b) => a + b, 0);
  const max = Math.max(...row);
  const min = Math.min(...row);
  const s2 = sum * sum;
  const l2 = length * length;
  return Math.max((l2 * max) / s2, s2 / (l2 * min));
}

/** Lay `row` along the shorter side of `box`; returns the remaining box. */
function layoutRow(row: { item: TreemapItem; area: number }[], box: Box, out: TreemapRect[]): Box {
  const sum = row.reduce((a, r) => a + r.area, 0);
  if (box.w >= box.h) {
    const colW = sum / box.h;
    let y = box.y;
    for (const r of row) {
      const h = r.area / colW;
      out.push({ key: r.item.key, value: r.item.value, x: box.x, y, w: colW, h });
      y += h;
    }
    return { x: box.x + colW, y: box.y, w: box.w - colW, h: box.h };
  }
  const rowH = sum / box.w;
  let x = box.x;
  for (const r of row) {
    const w = r.area / rowH;
    out.push({ key: r.item.key, value: r.item.value, x, y: box.y, w, h: rowH });
    x += w;
  }
  return { x: box.x, y: box.y + rowH, w: box.w, h: box.h - rowH };
}

/** Squarified treemap (Bruls et al.). Items with value <= 0 are dropped. */
export function squarify(items: TreemapItem[], width: number, height: number): TreemapRect[] {
  const valid = items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value);
  const total = valid.reduce((s, i) => s + i.value, 0);
  if (!valid.length || width <= 0 || height <= 0 || total <= 0) return [];
  const scale = (width * height) / total;
  const queue = valid.map((item) => ({ item, area: item.value * scale }));
  const out: TreemapRect[] = [];
  let box: Box = { x: 0, y: 0, w: width, h: height };
  let row: typeof queue = [];
  while (queue.length) {
    const next = queue[0];
    const length = Math.min(box.w, box.h);
    const areas = row.map((r) => r.area);
    if (!row.length || worst([...areas, next.area], length) <= worst(areas, length)) {
      row.push(next);
      queue.shift();
    } else {
      box = layoutRow(row, box, out);
      row = [];
    }
  }
  if (row.length) layoutRow(row, box, out);
  return out;
}
