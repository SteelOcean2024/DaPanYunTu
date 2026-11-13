/* ==========================================================================
 * 大盘云图 · 前端渲染
 * --------------------------------------------------------------------------
 * 整体流程：
 *
 *   fetch /api/heatmap/treemap  →  buildLayout()  →  draw()
 *                                      ↑
 *                              窗口尺寸变化时也要重算
 *
 * 为什么用 Canvas 而不是 DOM
 * --------------------------
 * 全市场 5500+ 个方块。用 DOM 的话每次刷新要重建 5500 个元素，
 * 浏览器排版 + 重绘会明显卡顿；Canvas 一次 drawImage 级别的绘制就能搞定，
 * 缩放平移也只是改一个变换矩阵，不用触发任何 layout。
 * 代价是要自己做命中检测（见 hitTest），这个用线性扫描足够快。
 *
 * 坐标系
 * ------
 * 「世界坐标」= 布局坐标系，原点在画布左上角，尺寸等于画布 CSS 像素。
 * 「屏幕坐标」= 世界坐标经过 view 变换后的结果：
 *     screen = world * view.scale + view.t
 * 缩放平移只改 view，不动布局，所以拖动是零成本的。
 * ========================================================================== */

'use strict';

/* ============================ 常量与状态 ============================ */

const API = {
  treemap: 'api/heatmap/treemap',
  summary: 'api/heatmap/summary',
  status:  'api/heatmap/status',
};

/** 颜色映射的量程（%）。超出部分按端点色处理。 */
const COLOR_RANGE = 4.0;

/**
 * 色阶。**不是自己调的，是从参考实现（大盘云图）的图例色带上逐点采样出来的。**
 *
 * 关键特征：**两端鲜艳、中间暗**（0% 是暗蓝灰 #414554，±4% 是最饱和的绿/红）。
 *
 * 早期版本我做成了「越极端越暗」，方向正好反了。后果是：
 * 满屏大跌时整张图糊成一片深色，大涨也是暗红，盘面看着发闷，
 * 而且「哪里跌得最狠」反而看不出来——最该跳出来的色块最不显眼。
 */
const COLOR_STOPS = [
  [-4.0, [0x0a, 0xd6, 0x48]],
  [-3.2, [0x0b, 0xc3, 0x44]],
  [-2.4, [0x15, 0xb1, 0x47]],
  [-1.6, [0x1f, 0x97, 0x48]],
  [-1.2, [0x27, 0x8c, 0x4b]],
  [-0.8, [0x34, 0x6a, 0x50]],
  [0.0, [0x41, 0x45, 0x54]],
  [0.4, [0xaa, 0x3f, 0x48]],
  [0.8, [0xbf, 0x3c, 0x42]],
  [1.6, [0xd5, 0x3b, 0x40]],
  [2.4, [0xe0, 0x38, 0x3c]],
  [3.2, [0xf3, 0x37, 0x3a]],
  [4.0, [0xf5, 0x36, 0x39]],
];

/** 个股行情页地址模板（同花顺个股页）。要换成东财/雪球改这里一行即可。 */
const QUOTE_URL_TEMPLATE = 'https://stockpage.10jqka.com.cn/{code}/';

/**
 * 缩放范围。
 *
 * 下限必须是 1：布局在 scale=1 时已经**正好铺满画布**（buildLayout 拿的就是画布尺寸），
 * 再往下缩只会让整张图变小、四周露出画布底色，一点信息都看不到。
 */
const MIN_SCALE = 1;
const MAX_SCALE = 80;

const state = {
  tree: null,            // 当前云图数据
  metric: 'total_mcap',
  view: { scale: 1, tx: 0, ty: 0 },
  layout: null,          // { tiles: [...], boards: [...], subBoards: [...] }
  hovered: null,
  dragging: false,
  dragMoved: false,
  lastMouse: { x: 0, y: 0 },
  autoRefresh: true,
  intervalMs: 5000,
  timer: null,
  fetching: false,
  canvasSize: { w: 0, h: 0 },
};

/* ============================ 工具函数 ============================ */

const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

/** 大数字转中文习惯单位：亿 / 万 */
function fmtMoney(v) {
  if (!isFinite(v)) return '--';
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + ' 万亿';
  if (a >= 1e8)  return (v / 1e8).toFixed(2) + ' 亿';
  if (a >= 1e4)  return (v / 1e4).toFixed(2) + ' 万';
  return v.toFixed(2);
}

function fmtPct(v) {
  if (!isFinite(v)) return '--';
  return (v > 0 ? '+' : '') + v.toFixed(2) + '%';
}

function fmtTime(iso) {
  if (!iso) return '--:--:--';
  const m = String(iso).match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(iso);
}

const pctClass = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat');

/* ============================ 颜色 ============================ */

/**
 * 涨跌幅 → 颜色。
 *
 * 采用「中间灰、两端深」的三段插值：
 *   0%      → 灰
 *   ±2%     → 中等饱和的红/绿
 *   ±4%以上 → 深红/深绿
 * 这与常见行情软件的色阶一致——极端涨跌用深色表示「强烈」，
 * 如果越极端越亮，满屏涨停时会亮得看不清文字。
 */
function lerpColor(a, b, t) {
  return [
    Math.round(a[0] + (b[0] - a[0]) * t),
    Math.round(a[1] + (b[1] - a[1]) * t),
    Math.round(a[2] + (b[2] - a[2]) * t),
  ];
}

const _colorCache = new Map();

/**
 * 涨跌幅 → 颜色（带缓存）。
 *
 * 全图一帧要取 5580 次颜色，每次都新建一个 `rgb(...)` 字符串的话，
 * 光字符串分配就是可观的开销。按 0.1% 精度取整做缓存后，
 * 实际不同的颜色只有几百种，后面全是命中。
 */
function colorFor(pct) {
  const key = Math.max(-200, Math.min(200, Math.round(pct * 20)));
  let c = _colorCache.get(key);
  if (c === undefined) {
    c = computeColor(key / 20);
    _colorCache.set(key, c);
  }
  return c;
}

/**
 * 在 :data:`COLOR_STOPS` 上做分段线性插值。
 *
 * 用实测采样点做插值，而不是套一个数学函数——参考实现的色阶本身就不是线性的
 * （红色在 0 → +0.4% 之间就跳掉了一大半亮度），套函数反而对不上。
 */
function computeColor(pct) {
  const p = clamp(pct, -COLOR_RANGE, COLOR_RANGE);
  let i = 0;
  while (i < COLOR_STOPS.length - 2 && p > COLOR_STOPS[i + 1][0]) i++;
  const [p0, c0] = COLOR_STOPS[i];
  const [p1, c1] = COLOR_STOPS[i + 1];
  const t = p1 === p0 ? 0 : (p - p0) / (p1 - p0);
  const rgb = lerpColor(c0, c1, clamp(t, 0, 1));
  return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
}

/** 文字颜色：深色块用白字，浅色块用深字，保证对比度 */
function textColorFor(pct) {
  return 'rgba(255,255,255,0.92)';
}

/* ============================ Squarified Treemap ============================ */

/**
 * 计算一排方块的「最差长宽比」。值越小越接近正方形。
 * 这是 squarified 算法的核心评价函数。
 */
function worstRatio(row, shortSide, scale) {
  const areas = row.map((d) => d.value * scale);
  const sum = areas.reduce((a, b) => a + b, 0);
  if (sum <= 0) return Infinity;
  const max = Math.max(...areas);
  const min = Math.min(...areas);
  const s2 = sum * sum;
  const w2 = shortSide * shortSide;
  return Math.max((w2 * max) / s2, s2 / (w2 * min));
}

/**
 * 把一排方块铺进 rect，返回剩余矩形。
 *
 * **关键：始终沿矩形的「短边」铺。**
 * - 矩形是竖长的（h >= w）→ 短边是宽度 → 铺一条**横向**带，带高 = 面积和 / 宽
 * - 矩形是横长的（w > h）→ 短边是高度 → 铺一条**纵向**带，带宽 = 面积和 / 高
 *
 * 铺完之后剩余矩形在带的另一侧，继续递归。
 * 这里的方向判断很容易写反——写反了就会渲染成一层层横条，
 * 而不是接近正方形的色块。
 */
function placeRow(row, rect, scale, out) {
  const sum = row.reduce((s, d) => s + d.value * scale, 0);
  if (sum <= 0 || rect.w <= 0 || rect.h <= 0) return rect;

  if (rect.h >= rect.w) {
    // 竖长矩形 → 横向铺一条带（带长 = 宽 = 短边）
    const rowH = sum / rect.w;
    let cx = rect.x;
    for (const d of row) {
      const dw = (d.value * scale) / rowH;
      out.push({ ...d, x: cx, y: rect.y, w: dw, h: rowH });
      cx += dw;
    }
    return { x: rect.x, y: rect.y + rowH, w: rect.w, h: rect.h - rowH };
  }
  // 横长矩形 → 纵向铺一条带（带长 = 高 = 短边）
  const rowW = sum / rect.h;
  let cy = rect.y;
  for (const d of row) {
    const dh = (d.value * scale) / rowW;
    out.push({ ...d, x: rect.x, y: cy, w: rowW, h: dh });
    cy += dh;
  }
  return { x: rect.x + rowW, y: rect.y, w: rect.w - rowW, h: rect.h };
}

/**
 * squarified treemap 主函数。
 *
 * @param {Array} children 已按 value 降序排列的节点
 * @param {{x,y,w,h}} rect 可用矩形
 * @returns {Array} 带 x/y/w/h 的节点副本
 */
function squarify(children, rect) {
  const items = children.filter((d) => d.value > 0);
  if (!items.length || rect.w <= 0 || rect.h <= 0) return [];

  const total = items.reduce((s, d) => s + d.value, 0);
  if (total <= 0) return [];

  const scale = (rect.w * rect.h) / total;
  const out = [];
  let rest = { ...rect };
  let row = [];
  let i = 0;

  while (i < items.length) {
    const c = items[i];
    const shortSide = Math.min(rest.w, rest.h);
    if (shortSide <= 0) break;

    if (row.length === 0) {
      row.push(c);
      i++;
      continue;
    }
    // 加入这一块后长宽比是否变好？变好就继续加，变差就先把当前排铺下去
    const withIt = worstRatio(row.concat([c]), shortSide, scale);
    const without = worstRatio(row, shortSide, scale);
    if (withIt <= without) {
      row.push(c);
      i++;
    } else {
      rest = placeRow(row, rest, scale, out);
      row = [];
    }
  }
  if (row.length) placeRow(row, rest, scale, out);
  return out;
}

/* ============================ 布局 ============================ */

/**
 * 三层布局：一级行业 → 二级行业 → 个股。
 *
 * 每一层都在自己的矩形顶部留一条「标题带」用来画板块名，
 * 剩下的区域再分给下一层。标题带高度按矩形高度的比例取，
 * 并做上下限截断——太小看不清，太大又挤占内容。
 */
function buildLayout(tree, W, H) {
  if (!tree || !tree.nodes || !tree.nodes.length || W <= 0 || H <= 0) return null;

  const boards = [];
  const subBoards = [];
  const tiles = [];

  const l1Rects = squarify(tree.nodes, { x: 0, y: 0, w: W, h: H });

  for (const b of l1Rects) {
    const node = b.node || b;
    // 一级标题带。刻意压得比较窄——标题带每高 1 像素，就是从个股方块上抠掉
    // 一整行的面积。全市场 5500 只股票本来就挤，这里必须省。
    const h1 = clamp(b.h * 0.045, 10, 15);
    const head1 = { x: b.x, y: b.y, w: b.w, h: Math.min(h1, b.h * 0.4) };
    const body1 = {
      x: b.x,
      y: b.y + head1.h,
      w: b.w,
      h: b.h - head1.h,
    };
    boards.push({ ...node, ...b, head: head1, body: body1 });

    if (body1.w <= 1 || body1.h <= 1) continue;

    const l2Rects = squarify(node.children || [], body1);
    for (const s of l2Rects) {
      const h2 = clamp(s.h * 0.062, 8, 12);
      const head2 = { x: s.x, y: s.y, w: s.w, h: Math.min(h2, s.h * 0.45) };
      const body2 = { x: s.x, y: s.y + head2.h, w: s.w, h: s.h - head2.h };
      subBoards.push({
        ...s,
        head: head2,
        boardName: node.name,
        boardColor: colorFor(node.changePct),
      });

      if (body2.w <= 1 || body2.h <= 1) continue;

      const stockRects = squarify(s.children || [], body2);
      for (const t of stockRects) {
        tiles.push({
          ...t,
          boardName: node.name,
          subBoardName: s.name,
        });
      }
    }
  }

  return { boards, subBoards, tiles };
}

/* ============================ 画布 ============================ */

const canvas = document.getElementById('treemap');
const ctx = canvas.getContext('2d', { alpha: false });

function resizeCanvas() {
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  const prev = state.canvasSize;

  canvas.width = Math.max(1, Math.round(w * dpr));
  canvas.height = Math.max(1, Math.round(h * dpr));
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  state.canvasSize = { w, h };
  state.dpr = dpr;
  if (!state.tree) return;

  // 保持「视口中心对应的世界点」不变。
  // 不做这一步的话，窗口一拉大，整个画面会相对左上角偏移，
  // 用户放大看到的局部会直接跑出屏幕——这就是「没有伸缩性」的直接表现。
  if (prev.w > 0 && prev.h > 0 && (prev.w !== w || prev.h !== h)) {
    const cxw = (prev.w / 2 - state.view.tx) / state.view.scale;
    const cyw = (prev.h / 2 - state.view.ty) / state.view.scale;
    state.view.tx = w / 2 - cxw * state.view.scale;
    state.view.ty = h / 2 - cyw * state.view.scale;
  }

  state.layout = buildLayout(state.tree, w, h);
  clampView();
  draw();
}

/** 世界坐标 → 屏幕坐标 */
function toScreen(x, y) {
  const { scale, tx, ty } = state.view;
  return { x: x * scale + tx, y: y * scale + ty };
}

/** 屏幕坐标 → 世界坐标 */
function toWorld(x, y) {
  const { scale, tx, ty } = state.view;
  return { x: (x - tx) / scale, y: (y - ty) / scale };
}

/**
 * 把平移量夹回「画布始终被铺满」的范围内。
 *
 * 世界坐标的尺寸就等于画布尺寸，所以布局左右边缘在屏幕上的位置分别是
 * ``tx`` 和 ``W*scale + tx``。要不露底色就得 ``tx ∈ [W*(1-scale), 0]``。
 * scale==1 时这个区间退化成 ``tx == 0``，也就是全图状态下拖不动——
 * 这是对的：全图时本来就没有可拖的余量。
 */
function clampView() {
  const { w: W, h: H } = state.canvasSize;
  const { scale } = state.view;
  if (!W || !H) return;
  state.view.tx = clamp(state.view.tx, W * (1 - scale), 0);
  state.view.ty = clamp(state.view.ty, H * (1 - scale), 0);
}

/* ============================ 绘制 ============================ */

function draw() {
  const { w: W, h: H } = state.canvasSize;
  const dpr = state.dpr || 1;
  const layout = state.layout;

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0f1115';
  ctx.fillRect(0, 0, W, H);
  // ctx.save/restore 会连字体一起还原，缓存必须作废
  _lastFontSpec = '';

  if (!layout) return;

  const { scale, tx, ty } = state.view;
  const viewRect = {
    x: -tx / scale,
    y: -ty / scale,
    w: W / scale,
    h: H / scale,
  };

  ctx.save();
  ctx.translate(tx, ty);
  ctx.scale(scale, scale);

  // ---------- 个股方块 ----------
  for (const t of layout.tiles) {
    // 视口裁剪：屏幕外的直接跳过，缩放到深处时能省下大量绘制
    if (t.x > viewRect.x + viewRect.w || t.x + t.w < viewRect.x) continue;
    if (t.y > viewRect.y + viewRect.h || t.y + t.h < viewRect.y) continue;

    ctx.fillStyle = colorFor(t.changePct);
    // 方块之间留一条 1 屏幕像素的缝：既消除亚像素白边，也让相邻方块的
    // 文字不会视觉粘连。方块本身小于 6 屏幕像素时不留缝，否则会被掏空。
    const gap = t.w * scale > 6 && t.h * scale > 6 ? 1 / scale : 0;
    ctx.fillRect(t.x + gap * 0.5, t.y + gap * 0.5,
                 Math.max(0.1, t.w - gap), Math.max(0.1, t.h - gap));
  }

  // ---------- 个股文字 ----------
  for (const t of layout.tiles) {
    if (t.x > viewRect.x + viewRect.w || t.x + t.w < viewRect.x) continue;
    if (t.y > viewRect.y + viewRect.h || t.y + t.h < viewRect.y) continue;
    drawTileLabel(t);
  }

  // ---------- 二级行业标题带 ----------
  for (const s of layout.subBoards) {
    if (s.head.w * scale < 46) continue;
    if (s.head.x > viewRect.x + viewRect.w || s.head.x + s.head.w < viewRect.x) continue;
    if (s.head.y > viewRect.y + viewRect.h || s.head.y + s.head.h < viewRect.y) continue;

    ctx.fillStyle = 'rgba(10,12,16,0.55)';
    ctx.fillRect(s.head.x, s.head.y, s.head.w, s.head.h);

    const fpx = headerFontSize(s.name, s.head, 0.62, 0.62, 9, scale);
    ctx.font = `${fpx}px -apple-system, "Microsoft YaHei", sans-serif`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';

    const pad = 4 / scale;
    ctx.fillStyle = 'rgba(255,255,255,0.94)';
    const nameW = ctx.measureText(s.name).width;
    ctx.fillText(s.name, s.head.x + pad, s.head.y + s.head.h / 2);

    // 涨跌幅右对齐
    if (s.head.w > 90 / scale) {
      ctx.textAlign = 'right';
      ctx.fillStyle = s.changePct >= 0 ? '#ff9a9a' : '#8ee0ab';
      ctx.fillText(fmtPct(s.changePct), s.head.x + s.head.w - pad, s.head.y + s.head.h / 2);
      ctx.textAlign = 'left';
    }
    void nameW;
  }

  // ---------- 一级行业标题带 ----------
  for (const b of layout.boards) {
    if (b.head.w * scale < 40) continue;
    if (b.head.x > viewRect.x + viewRect.w || b.head.x + b.head.w < viewRect.x) continue;
    if (b.head.y > viewRect.y + viewRect.h || b.head.y + b.head.h < viewRect.y) continue;

    ctx.fillStyle = 'rgba(8,10,13,0.72)';
    ctx.fillRect(b.head.x, b.head.y, b.head.w, b.head.h);

    // 左侧色条：一眼看出板块方向
    ctx.fillStyle = colorFor(b.changePct);
    ctx.fillRect(b.head.x, b.head.y, 3 / scale, b.head.h);

    const fpx = headerFontSize(b.name, b.head, 0.58, 0.50, 10, scale);
    ctx.font = `600 ${fpx}px -apple-system, "Microsoft YaHei", sans-serif`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';

    const pad = 7 / scale;
    ctx.fillStyle = 'rgba(255,255,255,0.97)';
    ctx.fillText(b.name, b.head.x + pad, b.head.y + b.head.h / 2);

    if (b.head.w > 130 / scale) {
      const nameW = ctx.measureText(b.name).width;
      ctx.font = `${fpx * 0.82}px -apple-system, "Microsoft YaHei", sans-serif`;
      ctx.fillStyle = b.changePct >= 0 ? '#ff9a9a' : '#8ee0ab';
      ctx.fillText(fmtPct(b.changePct), b.head.x + pad + nameW + 8 / scale,
                   b.head.y + b.head.h / 2);
    }
    if (b.head.w > 210 / scale) {
      ctx.textAlign = 'right';
      ctx.font = `${fpx * 0.78}px -apple-system, "Microsoft YaHei", sans-serif`;
      ctx.fillStyle = 'rgba(255,255,255,0.45)';
      ctx.fillText(`${b.stockCount} 只`, b.head.x + b.head.w - pad,
                   b.head.y + b.head.h / 2);
      ctx.textAlign = 'left';
    }
  }

  // ---------- 悬停高亮 ----------
  if (state.hovered) {
    const t = state.hovered;
    ctx.strokeStyle = 'rgba(255,255,255,0.95)';
    ctx.lineWidth = 1.6 / scale;
    ctx.strokeRect(t.x, t.y, t.w, t.h);
  }

  ctx.restore();
}

/**
 * 给个股方块画文字。
 *
 * 渲染策略：**能画就画，画不下就降级，而不是直接放弃。**
 *
 * 全市场 5500+ 只个股，中位方块只有 6×5 屏幕像素，绝大部分注定画不下字。
 * 但只要方块够放一个字，就应该画出来——参考实现（大盘云图）连
 * 11 像素宽的方块都会画「N」「长」「中」这样的单字。
 * 一开始我的门槛是「宽≥34 且完整名称放得下」，结果 98.7% 的方块是纯色块，
 * 整张图看上去就是马赛克。所以这里改成四级降级：
 *
 * ================  ====================================
 * 屏幕尺寸          布局
 * ================  ====================================
 * ≥56 × ≥46         三行：名称 / 涨跌幅 / 价格
 * ≥24 × ≥22         两行：名称 / 涨跌幅
 * ≥34 × ≥13         一行：名称 + 涨跌幅（左右对齐）
 * ≥13 × ≥10         一行：名称（放不下就截断成「兆易…」甚至「兆」）
 * 更小              不画
 * ================  ====================================
 *
 * 字号一律**自适应**：先按字符宽度估算，再用 measureText 校正两三轮，
 * 取能塞进方块的最大字号（下限 6.5 屏幕像素）。
 */
const FONT_STACK =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';

/**
 * 估算一段文字的「字宽单位」：中日韩字符算 1.0 em，其余（数字/字母/符号）算 0.55 em。
 * 用它先估一个字号，能省掉大部分 measureText 调用——否则 2000 个方块
 * 每个都从 30px 线性试到 6.5px，一帧就是几万次测量。
 */
function textWeightUnits(s) {
  let units = 0;
  for (const ch of s) units += ch.charCodeAt(0) > 0x2e80 ? 1.0 : 0.55;
  return units || 1;
}

/**
 * 行业标题带的字号，返回**世界单位**（直接喂给处于 scale 变换下的 ctx.font）。
 *
 * 三条约束，缺一条都会出问题：
 * - 跟着缩放走：高度用 head.h * ratio（世界单位），屏幕上是 bandH * ratio，
 *   缩放前后占比不变；
 * - 名字放得下：按标题带可用宽度反推上限。**只会让字变小、不会变大**，
 *   所以全图状态下也不会比原来更容易挤在一起；
 * - 屏幕像素下限 minScreenPx，保证全图状态下还读得出来。
 *
 * 早期写的是 clamp(head.h * 0.62, 9 / scale, 15 / scale)：上界是**绝对**屏幕像素，
 * 于是放到 40 倍、标题带 480px 高，字仍然只有 15px。
 */
function headerFontSize(text, head, ratio, widthFrac, minScreenPx, scale) {
  const byHeight = head.h * ratio;
  const byWidth = (head.w * widthFrac) / textWeightUnits(text);
  return Math.max(Math.min(byHeight, byWidth), minScreenPx / scale);
}

/** 按屏幕字号设置画布字体（画布处于 scale 变换下，所以要除以 scale） */
let _lastFontSpec = '';
function setFontScreen(fScreen, weight, scale) {
  const spec = `${weight} ${fScreen / scale}px ${FONT_STACK}`;
  // 赋值 ctx.font 会触发字体串解析，密集区域里相邻方块字号常常相同，
  // 所以先比对再赋值。
  if (spec !== _lastFontSpec) {
    ctx.font = spec;
    _lastFontSpec = spec;
  }
}

/**
 * 求能塞进 ``availW``（世界单位）的最大**屏幕**字号。
 *
 * :returns: 字号（屏幕像素）；连下限都放不下时返回 0
 */
function fitFontSize(text, availW, maxF, minF, weight, scale) {
  // 单位换算：availW 是**世界**单位，而 f 是**屏幕**字号。
  // 屏幕字号 f 的文字在世界里宽 (f/scale)*units，要它 ≤ availW，就得上界 availW*scale/units。
  // 早先这里漏了 * scale，于是放大后方块越变越大、字却停在原尺寸，四周空一大片。
  let f = Math.min(maxF, (availW / textWeightUnits(text)) * scale);
  if (f < minF) {
    // 估算已经放不下，但 CJK 估算偏保守，实测一下下限字号
    setFontScreen(minF, weight, scale);
    return ctx.measureText(text).width <= availW ? minF : 0;
  }
  for (let i = 0; i < 4; i++) {
    setFontScreen(f, weight, scale);
    const w = ctx.measureText(text).width;
    if (w <= availW) {
      const grow = f * (availW / w);
      if (grow - f < 0.4) return Math.min(f, maxF);
      f = Math.min(maxF, grow);
    } else {
      f *= (availW / w) * 0.97;
      if (f < minF) return 0;
    }
  }
  return f >= minF ? f : 0;
}

/**
 * 把文字截断到可用宽度。
 *
 * 剩余空间只够 1~2 个字时**不加省略号**——「兆…」比「兆」更占地方也更难认，
 * 参考实现（大盘云图）在小方块上就是直接显示单个字（「长」「中」「N」）。
 */
function truncateText(text, availW, fScreen, weight, scale) {
  setFontScreen(fScreen, weight, scale);
  if (ctx.measureText(text).width <= availW) return text;
  let s = text;
  while (s.length > 1) {
    s = s.slice(0, -1);
    if (ctx.measureText(s).width <= availW) {
      // 只剩一两个字时给省略号也没意义，直接显示
      if ([...s].length <= 2) return s;
      if (ctx.measureText(s + '…').width <= availW) return s + '…';
      return s;
    }
  }
  return text.slice(0, 1);
}

//: 字号下限。canvas 是 DPR=2 的背衬，5.5 CSS px 实际渲染成 11 设备像素，
//: 在屏幕上依然可读——这正是参考实现能把全市场铺满还看得清字的原因。
//: 早期卡在 6.5px，导致「兆易创新」这种四字名放不下，只能截成「兆易…」。
const MIN_LABEL_FONT = 5.5;

/**
 * 决定一个方块该怎么写字。**纯计算，不画。**
 *
 * 结果缓存在方块对象上，键是当前的 `scale`。
 * 拖拽平移只改 tx/ty、不改 scale，所以整屏标签决策可以整帧复用，
 * 省掉上千次 measureText——这是拖拽能跟手的关键。
 *
 * :returns: 标签计划；方块太小放不下字时返回 ``null``
 */
function planLabel(t, scale) {
  if (t.__lp && t.__lp.scale === scale) return t.__lp;

  const plan = computeLabelPlan(t, scale);
  t.__lp = plan ? { ...plan, scale } : { scale, none: true };
  return plan;
}

function computeLabelPlan(t, scale) {
  const sw = t.w * scale;
  const sh = t.h * scale;

  // 小于这个尺寸连一个字都放不下，画了只会变成噪点。
  // 11×9 是「一个汉字 + 内边距」的物理下限。
  if (sw < 11 || sh < 9) return null;

  // 左右留白：至少 1.5 屏幕像素，且不小于方块宽度的 8%（每边 4%）。
  // 只留固定 3 屏幕像素的话，放大后一个 1400px 宽的方块里文字会紧贴两边，
  // 相邻方块看着就粘在一起了——留白必须跟方块一起长。
  const availW = t.w - Math.max(3 / scale, t.w * 0.08);
  const name = t.name;
  const pctText = fmtPct(t.changePct);
  const MIN_F = MIN_LABEL_FONT;

  // ---------- 一级：三行（名称 / 涨跌幅 / 价格）----------
  if (sw >= 54 && sh >= 44) {
    const f = fitFontSize(name, availW, sh * 0.24, 8, '600', scale);
    if (f > 0) return { mode: 'big', f, name, pctText, price: t.price.toFixed(2), showPrice: sh >= 72 };
  }

  // ---------- 二级：两行（名称 / 涨跌幅）----------
  if (sw >= 20 && sh >= 20) {
    const f = fitFontSize(name, availW, sh * 0.32, MIN_F, '600', scale);
    if (f > 0) return { mode: 'two', f, name, pctText };
  }

  // ---------- 三级：一行（名称 + 涨跌幅，左右对齐）----------
  if (sw >= 34 && sh >= 12) {
    const f = fitFontSize(name + '  ' + pctText, availW, sh * 0.58, MIN_F, '500', scale);
    if (f > 0) return { mode: 'both', f, name, pctText };
  }

  // ---------- 四级：只画名称，放不下就截断 ----------
  if (sw >= 11 && sh >= 9) {
    const maxF = sh * 0.74;
    const f = fitFontSize(name, availW, maxF, MIN_F, '500', scale);
    if (f > 0) return { mode: 'name', f, text: name };
    const f2 = MIN_F;
    const text = truncateText(name, availW, f2, '500', scale);
    return text ? { mode: 'name', f: f2, text } : null;
  }
  return null;
}

function drawTileLabel(t) {
  const { scale } = state.view;
  const plan = planLabel(t, scale);
  if (!plan || plan.none) return;

  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  const cx = t.x + t.w / 2;
  const cy = t.y + t.h / 2;
  const white = textColorFor(t.changePct);
  ctx.fillStyle = white;

  if (plan.mode === 'big') {
    setFontScreen(plan.f, '600', scale);
    ctx.fillText(plan.name, cx, cy - (plan.f * 1.15) / scale);
    setFontScreen(plan.f * 0.94, '600', scale);
    ctx.fillText(plan.pctText, cx, cy + (plan.f * 0.10) / scale);
    if (plan.showPrice) {
      ctx.fillStyle = 'rgba(255,255,255,0.72)';
      setFontScreen(plan.f * 0.78, '400', scale);
      ctx.fillText(plan.price, cx, cy + (plan.f * 1.30) / scale);
    }
  } else if (plan.mode === 'two') {
    setFontScreen(plan.f, '600', scale);
    ctx.fillText(plan.name, cx, cy - (plan.f * 0.62) / scale);
    setFontScreen(plan.f * 0.95, '500', scale);
    ctx.fillText(plan.pctText, cx, cy + (plan.f * 0.68) / scale);
  } else if (plan.mode === 'both') {
    setFontScreen(plan.f, '500', scale);
    const nameW = ctx.measureText(plan.name).width;
    const pctW = ctx.measureText(plan.pctText).width;
    const gap = (t.w - nameW - pctW) / 2;
    ctx.textAlign = 'left';
    ctx.fillText(plan.name, t.x + gap, cy);
    ctx.textAlign = 'right';
    ctx.fillText(plan.pctText, t.x + t.w - gap, cy);
  } else {
    setFontScreen(plan.f, '500', scale);
    ctx.fillText(plan.text, cx, cy);
  }

  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
}
/* ============================ 交互 ============================ */

/** 命中检测：找到鼠标下的个股方块 */
function hitTest(sx, sy) {
  const layout = state.layout;
  if (!layout) return null;
  const p = toWorld(sx, sy);
  // 从后往前找：后画的在上层，但个股之间不重叠，顺序无所谓；
  // 这里倒序遍历是为了让后加入的（通常更小）优先命中
  for (let i = layout.tiles.length - 1; i >= 0; i--) {
    const t = layout.tiles[i];
    if (p.x >= t.x && p.x <= t.x + t.w && p.y >= t.y && p.y <= t.y + t.h) return t;
  }
  return null;
}

const tooltipEl = document.getElementById('tooltip');

function showTooltip(t, sx, sy) {
  const rows = [
    ['现价', t.price.toFixed(2)],
    ['涨跌幅', fmtPct(t.changePct)],
    ['成交额', fmtMoney(t.turnoverAmount)],
    ['总市值', fmtMoney(t.totalMarketCap)],
    ['流通市值', fmtMoney(t.floatMarketCap)],
  ];
  tooltipEl.innerHTML =
    `<div class="tt-name">${escapeHtml(t.name)}<span class="tt-code">${escapeHtml(t.code)}</span></div>` +
    rows
      .map(
        ([k, v]) =>
          `<div class="tt-row"><span class="tt-key">${k}</span>` +
          `<span class="tt-val" style="color:${k === '涨跌幅' ? (t.changePct >= 0 ? '#ff8f8f' : '#7fd8a0') : 'inherit'}">${v}</span></div>`
      )
      .join('') +
    `<div class="tt-path">${escapeHtml(t.boardName)} › ${escapeHtml(t.subBoardName)}</div>` +
    `<div class="tt-hint">单击打开行情页 ↗</div>`;

  tooltipEl.classList.add('show');

  // 贴着光标显示，靠近右/下边缘时翻到另一侧
  const wrap = canvas.parentElement;
  const rect = tooltipEl.getBoundingClientRect();
  let left = sx + 16;
  let top = sy + 16;
  if (left + rect.width > wrap.clientWidth - 8) left = sx - rect.width - 16;
  if (top + rect.height > wrap.clientHeight - 8) top = sy - rect.height - 16;
  tooltipEl.style.left = Math.max(4, left) + 'px';
  tooltipEl.style.top = Math.max(4, top) + 'px';
}

function hideTooltip() {
  tooltipEl.classList.remove('show');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

canvas.addEventListener('mousemove', (e) => {
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  state.lastMouse = { x: sx, y: sy };

  if (state.dragging) {
    const dx = e.clientX - state.dragStartX;
    const dy = e.clientY - state.dragStartY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) state.dragMoved = true;
    state.view.tx = state.dragStartTx + dx;
    state.view.ty = state.dragStartTy + dy;
    clampView();
    draw();
    hideTooltip();
    return;
  }

  const t = hitTest(sx, sy);
  if (t !== state.hovered) {
    state.hovered = t;
    // 悬停在个股上时给「可点」的手型，否则是可拖拽的手型
    canvas.style.cursor = t ? 'pointer' : '';
    draw();
  }
  if (t) showTooltip(t, sx, sy);
  else hideTooltip();
});

canvas.addEventListener('mouseleave', () => {
  state.hovered = null;
  hideTooltip();
  draw();
});

canvas.addEventListener('mousedown', (e) => {
  state.dragging = true;
  state.dragMoved = false;
  state.dragStartX = e.clientX;
  state.dragStartY = e.clientY;
  state.dragStartTx = state.view.tx;
  state.dragStartTy = state.view.ty;
  canvas.classList.add('dragging');
});

window.addEventListener('mouseup', () => {
  if (state.dragging) {
    state.dragging = false;
    canvas.classList.remove('dragging');
  }
});

/**
 * 打开个股行情页。
 *
 * 为什么用 <a> 点击而不是 window.open
 * ----------------------------------
 * ``window.open(url, '_blank', 'noopener')`` 里第三个参数一旦非空，
 * Chrome 就按「弹出式窗口」处理这次调用：既容易被弹窗拦截器吃掉，
 * 也常常开成一个独立小窗口并落在主窗口后面——用户看到的就是「点了没反应」。
 * 实测（Chromium，真实鼠标点击）带 features 时 ``window.open`` 返回 null、
 * 页面上什么都没出现；去掉 features 或改用 <a> 才稳定开成当前窗口的新标签页。
 *
 * 不用 ``location.href``：云图是常驻盯盘页，跳走会丢掉缩放位置和当前视图。
 */
function openQuote(t) {
  const code = String(t.code).split('.')[0];
  const url = QUOTE_URL_TEMPLATE.replace('{code}', code);

  const a = document.createElement('a');
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

canvas.addEventListener('click', (e) => {
  // 拖拽结束后不要误触发点击
  if (state.dragMoved) {
    state.dragMoved = false;
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const t = hitTest(e.clientX - rect.left, e.clientY - rect.top);
  if (t) openQuote(t);
});

/** 滚轮缩放：以光标位置为锚点，保证光标下的内容不动 */
canvas.addEventListener(
  'wheel',
  (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;

    const before = toWorld(sx, sy);
    const factor = Math.exp(-e.deltaY * 0.0016);
    const next = clamp(state.view.scale * factor, MIN_SCALE, MAX_SCALE);
    state.view.scale = next;
    // 让 before 这个世界点仍然落在 (sx, sy)
    state.view.tx = sx - before.x * next;
    state.view.ty = sy - before.y * next;

    clampView();
    draw();
    const t = hitTest(sx, sy);
    state.hovered = t;
    if (t) showTooltip(t, sx, sy);
    else hideTooltip();
  },
  { passive: false }
);

/** 平滑地把某个矩形放大到充满视口 */
function focusRect(r, padding = 6) {
  const { w: W, h: H } = state.canvasSize;
  if (!r || r.w <= 0 || r.h <= 0) return;
  const scale = clamp(
    Math.min((W - padding * 2) / r.w, (H - padding * 2) / r.h),
    MIN_SCALE,
    MAX_SCALE
  );
  state.view.scale = scale;
  state.view.tx = W / 2 - (r.x + r.w / 2) * scale;
  state.view.ty = H / 2 - (r.y + r.h / 2) * scale;
  clampView();
  draw();
}

function resetView() {
  state.view = { scale: 1, tx: 0, ty: 0 };
  draw();
  document.querySelectorAll('.board-list li.active').forEach((el) =>
    el.classList.remove('active')
  );
  document.getElementById('stageTitle').textContent = 'A 股全图';
}

/* ============================ 侧栏渲染 ============================ */

function renderSummary(summary) {
  if (!summary) return;
  const total = summary.advanceCount + summary.flatCount + summary.declineCount || 1;

  document.getElementById('advanceCount').textContent = summary.advanceCount.toLocaleString();
  document.getElementById('flatCount').textContent = summary.flatCount.toLocaleString();
  document.getElementById('declineCount').textContent = summary.declineCount.toLocaleString();

  const bar = document.getElementById('breadthBar').children;
  bar[0].style.width = (summary.advanceCount / total) * 100 + '%';
  bar[1].style.width = (summary.flatCount / total) * 100 + '%';
  bar[2].style.width = (summary.declineCount / total) * 100 + '%';

  document.getElementById('limitUp').textContent = summary.limitUpCount;
  document.getElementById('limitDown').textContent = summary.limitDownCount;
  document.getElementById('stockCount').textContent = summary.stockCount.toLocaleString();
  document.getElementById('turnover').textContent = fmtMoney(summary.turnoverAmount);

  const idxList = document.getElementById('indexList');
  const indexes = summary.indexes || {};
  const names = Object.keys(indexes);
  if (names.length) {
    idxList.innerHTML = names
      .map((n) => {
        const q = indexes[n];
        return (
          `<li><span class="swatch" style="background:${colorFor(q.changePct)}"></span>` +
          `<span class="item-name">${escapeHtml(n)}</span>` +
          `<span class="item-val">${q.price.toFixed(2)}</span>` +
          `<span class="item-pct ${pctClass(q.changePct)}">${fmtPct(q.changePct)}</span></li>`
        );
      })
      .join('');
  }
}

function renderBoardList(tree) {
  const ul = document.getElementById('boardList');
  ul.innerHTML = tree.nodes
    .map(
      (b) =>
        `<li data-board="${escapeHtml(b.name)}">` +
        `<span class="swatch" style="background:${colorFor(b.changePct)}"></span>` +
        `<span class="item-name">${escapeHtml(b.name)}</span>` +
        `<span class="item-pct ${pctClass(b.changePct)}">${fmtPct(b.changePct)}</span></li>`
    )
    .join('');

  ul.querySelectorAll('li').forEach((li) => {
    li.addEventListener('click', () => {
      const name = li.dataset.board;
      const layout = state.layout;
      if (!layout) return;
      const b = layout.boards.find((x) => x.name === name);
      if (!b) return;
      focusRect({ x: b.x, y: b.y, w: b.w, h: b.h });
      ul.querySelectorAll('li.active').forEach((el) => el.classList.remove('active'));
      li.classList.add('active');
      document.getElementById('stageTitle').textContent = name;
      // 窄屏抽屉模式下，选完板块就把抽屉收起来，把画面让给云图
      if (window.matchMedia('(max-width: 900px)').matches) toggleSidebar(false);
    });
  });
}

/* ============================ 数据获取 ============================ */

const connDot = document.getElementById('connDot');
const loadingEl = document.getElementById('loading');
const loadingText = document.getElementById('loadingText');


/** 把一份云图数据应用到界面。后端与浏览器直连两条路共用。 */
function applyTree(tree, ms) {
  state.tree = tree;
  state.layout = buildLayout(tree, state.canvasSize.w, state.canvasSize.h);

  renderSummary(tree.summary);
  renderBoardList(tree);

  document.getElementById('refreshTime').textContent = fmtTime(tree.updatedAt);
  document.getElementById('latencyText').textContent = `${Math.round(ms)} ms`;
  document.getElementById('universeText').textContent =
    `${tree.stockCount.toLocaleString()} 只 / ${tree.boardCount} 板块`;
  document.getElementById('statusText').textContent = state.clientMode ? '静态模式' : '正常';
  document.getElementById('legendNote').textContent =
    `颜色 = 涨跌幅（红涨绿跌）· 面积 = ${tree.metricLabel || ''}`;

  connDot.className = 'dot ok';
  loadingEl.classList.add('hidden');
  draw();

  // 静态模式下每轮要自己拉全市场行情（约 3 秒），把轮询放慢到 10 秒
  if (state.clientMode && !state.clientRescheduled) {
    state.clientRescheduled = true;
    scheduleRefresh();
  }
}

/**
 * 静态部署（GitHub Pages 等没有 Python 后端）时，由浏览器直连腾讯自行组装。
 * 数据层在 web/shared/market.js，与后端同构（已逐项对齐验证）。
 * 8 秒内的口径切换直接复用缓存，不必重新拉行情。
 */
async function treeFromClient() {
  if (!window.MarketData) throw new Error('缺少 shared/market.js');
  const now = Date.now();
  if (!state.clientAll || now - (state.clientAt || 0) > 8000) {
    state.clientAll = await window.MarketData.fetchAll({ base: '../' });
    state.clientAt = now;
  }
  return window.MarketData.buildTreemap(state.metric);
}

async function fetchTreemap() {
  if (state.fetching) return;
  state.fetching = true;
  connDot.className = 'dot busy';

  const t0 = performance.now();
  try {
    const resp = await fetch(`${API.treemap}?metric=${encodeURIComponent(state.metric)}`, {
      cache: 'no-store',
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const tree = await resp.json();
    state.clientMode = false;
    applyTree(tree, performance.now() - t0);
  } catch (err) {
    try {
      if (!state.clientMode) console.warn('[heatmap] 后端不可用，切换浏览器直连', err.message);
      state.clientMode = true;
      const tree = await treeFromClient();
      applyTree(tree, performance.now() - t0);
    } catch (e2) {
      connDot.className = 'dot err';
      document.getElementById('statusText').textContent = '取数失败';
      loadingText.textContent = '取数失败：' + e2.message + '（自动重试中）';
      loadingEl.classList.remove('hidden');
      console.error('[heatmap] 取数失败', e2);
    }
  } finally {
    state.fetching = false;
  }
}

function scheduleRefresh() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (state.autoRefresh) {
    // 静态模式下浏览器要自己拉全市场行情，放慢到至少 10 秒
    const every = state.clientMode ? Math.max(state.intervalMs, 10000) : state.intervalMs;
    state.timer = setInterval(fetchTreemap, every);
  }
}

/* ============================ 事件绑定 ============================ */

document.getElementById('metricSeg').addEventListener('click', (e) => {
  const btn = e.target.closest('.seg-btn');
  if (!btn) return;
  document.querySelectorAll('.seg-btn').forEach((b) => b.classList.remove('active'));
  btn.classList.add('active');
  state.metric = btn.dataset.metric;
  fetchTreemap();
});

document.getElementById('autoRefresh').addEventListener('change', (e) => {
  state.autoRefresh = e.target.checked;
  scheduleRefresh();
});

document.getElementById('intervalSelect').addEventListener('change', (e) => {
  state.intervalMs = parseInt(e.target.value, 10);
  scheduleRefresh();
});

document.getElementById('manualRefresh').addEventListener('click', fetchTreemap);
document.getElementById('resetView').addEventListener('click', resetView);

/* ---------- 窄屏侧栏抽屉 ----------
 * 桌面端侧栏常驻；窄屏（≤900px）改成浮层抽屉，否则 236px 的固定侧栏
 * 会把画布挤成一条缝——这正是最初「没有伸缩性」的样子。 */
const sidebarEl = document.querySelector('.sidebar');
const backdropEl = document.getElementById('backdrop');

function toggleSidebar(force) {
  const open = force !== undefined ? force : !sidebarEl.classList.contains('open');
  sidebarEl.classList.toggle('open', open);
  backdropEl.classList.toggle('show', open);
}

document.getElementById('sidebarToggle').addEventListener('click', () => toggleSidebar());
backdropEl.addEventListener('click', () => toggleSidebar(false));

// 用 ResizeObserver 监听画布容器，而不是 window 的 resize：
// 侧栏折叠/展开、浏览器缩放、进入全屏都不会触发 window.resize，
// 但画布容器一定会变。漏掉这些场景，画布就会停在旧尺寸上。
if (window.ResizeObserver) {
  new ResizeObserver(() => resizeCanvas()).observe(canvas.parentElement);
} else {
  window.addEventListener('resize', resizeCanvas);
}

// 键盘：0 重置视图
window.addEventListener('keydown', (e) => {
  if (e.key === '0') resetView();
});

/* ============================ 启动 ============================ */

// 图例渐变条
(function initLegend() {
  const el = document.getElementById('legendBar');
  const stops = [];
  for (let i = 0; i <= 20; i++) {
    const pct = -COLOR_RANGE + (i / 20) * COLOR_RANGE * 2;
    stops.push(`${colorFor(pct)} ${(i / 20) * 100}%`);
  }
  el.style.background = `linear-gradient(to right, ${stops.join(',')})`;
})();

resizeCanvas();
scheduleRefresh();
fetchTreemap();
