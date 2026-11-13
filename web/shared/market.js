/* ==========================================================================
 * 大盘云图 · 浏览器直连数据层
 * --------------------------------------------------------------------------
 * 为什么需要它
 * ------------
 * 本地跑的时候，行情和板块指标都是 Python 后端算好的；但 GitHub Pages 这类
 * **纯静态托管跑不了 Python**，前端得自己取数。
 *
 * 关键点：腾讯的行情接口虽然不给 CORS 头，但它是「赋值给全局变量的脚本」
 * （`v_sh600000="..."`），可以用 <script> 标签加载——**script 标签不受同源策略限制**。
 * 所以浏览器能直接拿到全市场行情，实时这条链路在静态托管上照样成立。
 *
 * 代价（必须说清楚）
 * ----------------
 * 这里的解析与聚合是 src/heatmap/quotes.py + src/analysis/sector.py 的**第二份实现**。
 * 两份逻辑必须同步维护：Python 那份是权威（本地/服务器用），这份只服务静态部署。
 * 改动任何一边的口径，另一边要一起改。
 *
 * 口径与 Python 端完全一致：
 *   [3]  现价      [32] 涨跌幅(%)   [36] 成交量(手)  [37] 成交额(万元)
 *   [44] 流通市值(亿元)  [45] 总市值(亿元)  [47] 涨停价  [48] 跌停价
 * ========================================================================== */
(function (global) {
  "use strict";

  var QUOTE_URL = "https://qt.gtimg.cn/q=";
  var CHUNK = 600;
  var CONCURRENCY = 8;
  var MAP_URL = "data/industry-map.json";

  /* ------------------------------ 代码规范化 ------------------------------ */

  // 与 src/data/codes.py 的 exchange_of 保持一致。
  // 注意：指数代码不适用（399001 反推不出来，000001 会和平安银行撞车），
  // 指数一律走 Python 端，这里只处理个股。
  function exchangeOf(code) {
    var two = code.slice(0, 2), head = code.charAt(0);
    if (two === "60" || two === "68" || head === "5") return "sh";
    if (two === "00" || two === "30" || head === "1") return "sz";
    if (head === "4" || head === "8" || two === "92") return "bj";
    if (two === "90") return "sh";
    return null;
  }
  function toMarketCode(code) {
    var ex = exchangeOf(code);
    return ex ? ex + code : null;
  }

  function num(v) {
    var f = parseFloat(v);
    return isFinite(f) ? f : 0;
  }
  function medianOf(sorted) {
    var n = sorted.length;
    if (!n) return 0;
    var mid = n >> 1;
    return n % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  /* ------------------------------ 脚本加载 ------------------------------ */

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = url;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error("加载失败：" + url)); };
      document.head.appendChild(s);
    });
  }

  /* ------------------------------ 行情解析 ------------------------------ */

  function parseQuote(marketCode, raw) {
    if (!raw) return null;
    var fields = String(raw).replace(/^"|"$/g, "").split("~");
    if (fields.length < 49) return null;
    var price = num(fields[3]);
    if (price <= 0) return null;                       // 停牌/退市/无行情
    var ex = marketCode.slice(0, 2).toUpperCase();
    if (ex !== "SH" && ex !== "SZ" && ex !== "BJ") return null;
    var t = String(fields[30] || "");                  // 20261113150003
    var quoteTime = t.length === 14
      ? t.slice(0, 4) + "-" + t.slice(4, 6) + "-" + t.slice(6, 8) + "T" +
        t.slice(8, 10) + ":" + t.slice(10, 12) + ":" + t.slice(12, 14)
      : "";
    return {
      code: fields[2] || marketCode.slice(2),
      name: (fields[1] || "").trim(),
      exchange: ex,
      price: price,
      changePct: num(fields[32]),
      volume: num(fields[36]) * 100,
      amount: num(fields[37]) * 1e4,
      floatMcap: num(fields[44]) * 1e8,
      totalMcap: num(fields[45]) * 1e8,
      limitUp: num(fields[47]),
      limitDown: num(fields[48]),
      quoteTime: quoteTime,
    };
  }

  /**
   * 拉全市场行情。分批 + 并发上限，5580 只约 10 批、1~2 秒。
   *
   * :param onProgress: 可选，(done, total) => void
   */
  function fetchQuotes(marketCodeList, onProgress) {
    var batches = [];
    for (var i = 0; i < marketCodeList.length; i += CHUNK) {
      batches.push(marketCodeList.slice(i, i + CHUNK));
    }

    var out = [];
    var done = 0;
    var next = 0;

    function runOne() {
      var idx = next++;
      if (idx >= batches.length) return Promise.resolve();
      var batch = batches[idx];
      return loadScript(QUOTE_URL + batch.join(",")).then(function () {
        for (var k = 0; k < batch.length; k++) {
          var mc = batch[k];
          var raw = global["v_" + mc];
          var q = parseQuote(mc, raw);
          if (q) out.push(q);
          try { delete global["v_" + mc]; } catch (e) { global["v_" + mc] = undefined; }
        }
        done++;
        if (onProgress) onProgress(done, batches.length);
        return runOne();
      });
    }

    var lanes = [];
    for (var j = 0; j < Math.min(CONCURRENCY, batches.length); j++) lanes.push(runOne());
    return Promise.all(lanes).then(function () { return out; });
  }

  /* ------------------------------ 板块聚合 ------------------------------ */

  // 与 src/analysis/sector.py 的 _aggregate 逐条对应，改一边要改两边。
  function aggregate(name, level, rows, market) {
    var n = rows.length;
    var pcts = [];
    var mcapSum = 0, wpSum = 0, amt = 0, up = 0, down = 0, limitUp = 0;
    var leader = null;
    for (var i = 0; i < n; i++) {
      var r = rows[i];
      pcts.push(r.changePct);
      mcapSum += r.totalMcap;
      wpSum += r.totalMcap * r.changePct;
      amt += r.amount;
      if (r.changePct > 0) up++; else if (r.changePct < 0) down++;
      if (r.limitUp > 0 && r.price >= r.limitUp - 0.005) limitUp++;
      if (!leader || r.amount > leader.amount) leader = r;
    }
    pcts.sort(function (a, b) { return a - b; });
    var median = medianOf(pcts);
    var capw = mcapSum > 0 ? wpSum / mcapSum
      : (pcts.reduce(function (a, b) { return a + b; }, 0) / (n || 1));

    var rec = {
      name: name, level: level, stockCount: n,
      upCount: up, downCount: down, flatCount: n - up - down,
      limitUpCount: limitUp,
      capwPct: round(capw, 3), medianPct: round(median, 3),
      spread: round(capw - median, 3),
      breadth: n ? round(up / n, 4) : 0,
      amount: amt, totalMcap: mcapSum,
      leaderCode: leader ? leader.code : "",
      leaderName: leader ? leader.name : "",
      leaderPct: leader ? round(leader.changePct, 2) : 0,
    };
    if (!market) {
      rec.amountShare = 1.0; rec.rsCapw = 0.0; rec.rsMedian = 0.0;
    } else {
      rec.amountShare = market.amount > 0 ? round(amt / market.amount, 6) : 0;
      rec.rsCapw = round(capw - market.capwPct, 3);
      rec.rsMedian = round(median - market.medianPct, 3);
    }
    return rec;
  }
  function round(v, d) {
    var p = Math.pow(10, d);
    return Math.round(v * p) / p;
  }

  /**
   * 把行情 + 行业映射聚合成板块强度。
   *
   * :returns: 与 /api/sector/strength **同构**的 JSON
   */
  function computeSectorStrength(quotes, map, updatedAt, level) {
    var l1 = map.l1, l2 = map.l2, l2Parent = map.l2Parent;
    var l1Buckets = [], l2Buckets = [];
    for (var i = 0; i < l1.length; i++) l1Buckets.push([]);
    for (var j = 0; j < l2.length; j++) l2Buckets.push([]);

    var idx = {};
    for (var s = 0; s < map.stocks.length; s++) {
      idx[map.stocks[s][0]] = s;
    }

    // 未挂到行业的个股，归到「未分类」——丢掉会让家数之和对不上大盘
    var unclassified = [];
    var seenL1 = {}, seenL2 = {};
    for (var q = 0; q < quotes.length; q++) {
      var it = quotes[q];
      var pos = idx[it.code];
      if (pos === undefined) { unclassified.push(it); continue; }
      var a = map.stocks[pos][1], b = map.stocks[pos][2];
      l1Buckets[a].push(it);
      l2Buckets[b].push(it);
      seenL1[a] = 1; seenL2[b] = 1;
    }

    var market = aggregate("全市场", "market", quotes, null);
    var out = { updatedAt: updatedAt, market: market, l1: [], l2: [], source: "browser" };

    if (level !== "l2") {
      for (var x = 0; x < l1.length; x++) {
        if (!seenL1[x] || !l1Buckets[x].length) continue;
        out.l1.push(aggregate(l1[x], "l1", l1Buckets[x], market));
      }
      if (unclassified.length) {
        out.l1.push(aggregate("未分类", "l1", unclassified, market));
      }
      out.l1.sort(function (p, r2) { return r2.rsMedian - p.rsMedian; });
    }

    if (level !== "l1") {
      for (var y = 0; y < l2.length; y++) {
        if (!seenL2[y] || !l2Buckets[y].length) continue;
        out.l2.push(aggregate(l2[y], "l2", l2Buckets[y], market));
      }
      out.l2.sort(function (p, r2) { return r2.rsMedian - p.rsMedian; });
    }
    return out;
  }

  /* ------------------------------ 对外入口 ------------------------------ */

  var _mapCache = null;
  function loadIndustryMap(base) {
    if (_mapCache) return Promise.resolve(_mapCache);
    var url = (base || "") + MAP_URL;
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error("行业映射加载失败：" + r.status + " " + url);
      return r.json();
    }).then(function (d) { _mapCache = d; return d; });
  }

  /**
   * 静态部署下的完整取数：映射 → 全市场行情 → 板块强度。
   *
   * :param opts.base    行业映射所在目录（默认 "data/"，页面在 /sector/ 下用 ""）
   * :param opts.level   "l1" | "l2" | "both"
   * :param opts.onProgress (stage, done, total) => void
   */
  function fetchStrengthClient(opts) {
    opts = opts || {};
    var map, codes, latest = "";
    return loadIndustryMap(opts.base || "").then(function (m) {
      map = m;
      codes = [];
      for (var i = 0; i < m.stocks.length; i++) {
        var mc = toMarketCode(m.stocks[i][0]);
        if (mc) codes.push(mc);
      }
      if (opts.onProgress) opts.onProgress("quotes", 0, codes.length);
      return fetchQuotes(codes, function (d, t) {
        if (opts.onProgress) opts.onProgress("quotes", d, t);
      });
    }).then(function (quotes) {
      for (var i = 0; i < quotes.length; i++) {
        if (quotes[i].quoteTime > latest) latest = quotes[i].quoteTime;
      }
      // updatedAt 用「本次取数时间」，不是行情时间——前者是快照生成时刻，
      // 与 Python 端的语义一致（后端也是记组装时刻）。行情时间另放 quoteTime。
      var d = new Date();
      var pad = function (n) { return (n < 10 ? "0" : "") + n; };
      var stamp = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
                  "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
      var res = computeSectorStrength(quotes, map, stamp, opts.level || "both");
      res.quoteTime = latest;
      return res;
    });
  }
  /* ============================ 云图组装 ============================
   * 下面是 src/heatmap/treemap.py 的前端对应实现（第二份）。
   * 口径必须一致：面积 = metric 字段；涨跌幅一律按**面积值加权**，不是简单平均。
   * ================================================================ */

  var METRIC_LABEL = { total_mcap: "总市值", float_mcap: "流通市值", amount: "成交额" };
  var METRIC_FIELD = { total_mcap: "totalMcap", float_mcap: "floatMcap", amount: "amount" };

  var INDEX_CODES = {
    sh000001: "上证指数", sz399001: "深证成指", sz399006: "创业板指",
    sh000300: "沪深300", sh000688: "科创50", sh000905: "中证500",
    sh000852: "中证1000", bj899050: "北证50"
  };

  function fetchIndexes() {
    var codes = Object.keys(INDEX_CODES);
    return loadScript(QUOTE_URL + codes.join(",")).then(function () {
      var out = {};
      for (var i = 0; i < codes.length; i++) {
        var mc = codes[i];
        var raw = global["v_" + mc];
        try { delete global["v_" + mc]; } catch (e) { global["v_" + mc] = undefined; }
        if (!raw) continue;
        var f = String(raw).replace(/^"|"$/g, "").split("~");
        if (f.length < 33) continue;
        var price = num(f[3]);
        if (price <= 0) continue;
        out[INDEX_CODES[mc]] = { price: round(price, 2), changePct: round(num(f[32]), 2) };
      }
      return out;
    }).catch(function () { return {}; });   // 指数拉不到不该毁掉整个快照
  }

  function weightedChange(rows, field) {
    var n = rows.length, wsum = 0, psum = 0;
    for (var i = 0; i < n; i++) {
      var w = rows[i][field] || 0;
      wsum += w; psum += w * rows[i].changePct;
    }
    if (wsum <= 0) {
      if (!n) return 0;
      var s = 0;
      for (var j = 0; j < n; j++) s += rows[j].changePct;
      return s / n;                       // 权重全 0 时回落算术平均，避免除零
    }
    return psum / wsum;
  }
  function sumField(rows, field) {
    var s = 0;
    for (var i = 0; i < rows.length; i++) s += rows[i][field] || 0;
    return s;
  }
  function byValueDesc(a, b) { return b.value - a.value; }

  function stockNode(r, field) {
    return {
      code: r.code + "." + r.exchange,
      name: r.name,
      value: r[field],
      price: round(r.price, 3),
      changePct: round(r.changePct, 2),
      turnoverAmount: r.amount,
      totalMarketCap: r.totalMcap,
      floatMarketCap: r.floatMcap,
      boardName: "", subBoardName: "",
    };
  }

  var _cache = null;

  /**
   * 一次性把静态部署需要的东西全拉下来并缓存：
   * 行业映射 → 全市场行情 → 指数。之后切换面积口径不必重新取数。
   */
  function fetchAll(opts) {
    opts = opts || {};
    var base = opts.base || "";
    var map, quotes, indexes, codeIndex = {}, codes = [];
    return loadIndustryMap(base).then(function (m) {
      map = m;
      for (var i = 0; i < m.stocks.length; i++) {
        codeIndex[m.stocks[i][0]] = i;
        var mc = toMarketCode(m.stocks[i][0]);
        if (mc) codes.push(mc);
      }
      if (opts.onProgress) opts.onProgress("quotes", 0, codes.length);
      return Promise.all([
        fetchQuotes(codes, function (d, t) {
          if (opts.onProgress) opts.onProgress("quotes", d, t);
        }),
        fetchIndexes()
      ]);
    }).then(function (r) {
      quotes = r[0]; indexes = r[1];
      var latest = "";
      for (var i = 0; i < quotes.length; i++) {
        if (quotes[i].quoteTime > latest) latest = quotes[i].quoteTime;
      }
      var d = new Date();
      var pad = function (n) { return (n < 10 ? "0" : "") + n; };
      var stamp = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
                  "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
      _cache = {
        map: map, quotes: quotes, indexes: indexes, codeIndex: codeIndex,
        updatedAt: stamp, quoteTime: latest,
      };
      return _cache;
    });
  }

  /**
   * 组装云图 JSON。结构、字段名与后端 /api/heatmap/treemap **完全一致**。
   *
   * :param metric: "total_mcap" | "float_mcap" | "amount"
   */
  function buildTreemap(metric) {
    if (!_cache) throw new Error("请先调用 fetchAll()");
    var m = METRIC_FIELD[metric] ? metric : "total_mcap";
    var field = METRIC_FIELD[m];
    var map = _cache.map;

    var l2ByL1 = [];
    for (var a = 0; a < map.l1.length; a++) l2ByL1.push([]);
    for (var b = 0; b < map.l2.length; b++) l2ByL1[map.l2Parent[b]].push(b);

    var l1Rows = [], l2Rows = [];
    for (var i = 0; i < map.l1.length; i++) l1Rows.push([]);
    for (var j = 0; j < map.l2.length; j++) l2Rows.push([]);

    var kept = [];
    for (var q = 0; q < _cache.quotes.length; q++) {
      var it = _cache.quotes[q];
      var v = it[field];
      if (!(v > 0)) continue;              // 面积为 0 会让方块退化成一条线，剔除
      var pos = _cache.codeIndex[it.code];
      if (pos === undefined) continue;
      l1Rows[map.stocks[pos][1]].push(it);
      l2Rows[map.stocks[pos][2]].push(it);
      kept.push(it);
    }

    var nodes = [];
    for (var x = 0; x < map.l1.length; x++) {
      var g1 = l1Rows[x];
      if (!g1.length) continue;
      var subs = [];
      var kids = l2ByL1[x];
      for (var k = 0; k < kids.length; k++) {
        var g2 = l2Rows[kids[k]];
        if (!g2.length) continue;
        subs.push({
          code: "", name: map.l2[kids[k]],
          value: sumField(g2, field),
          changePct: round(weightedChange(g2, field), 2),
          stockCount: g2.length,
          children: g2.slice().sort(function (p, r) { return r[field] - p[field]; })
                     .map(function (r) { return stockNode(r, field); }),
        });
      }
      subs.sort(byValueDesc);
      nodes.push({
        code: "", name: map.l1[x],
        value: sumField(g1, field),
        changePct: round(weightedChange(g1, field), 2),
        stockCount: g1.length,
        children: subs,
      });
    }
    nodes.sort(byValueDesc);

    // 板块名补回每只票上（悬浮提示要用）
    for (var n1 = 0; n1 < nodes.length; n1++) {
      var l1n = nodes[n1];
      for (var n2 = 0; n2 < l1n.children.length; n2++) {
        var l2n = l1n.children[n2];
        for (var n3 = 0; n3 < l2n.children.length; n3++) {
          l2n.children[n3].boardName = l1n.name;
          l2n.children[n3].subBoardName = l2n.name;
        }
      }
    }

    var advance = 0, decline = 0, flat = 0, limitUp = 0, limitDown = 0;
    for (var z = 0; z < kept.length; z++) {
      var t = kept[z];
      if (t.changePct > 0) advance++; else if (t.changePct < 0) decline++; else flat++;
      if (t.limitUp > 0 && t.price >= t.limitUp - 0.005) limitUp++;
      if (t.limitDown > 0 && t.price <= t.limitDown + 0.005) limitDown++;
    }

    return {
      market: "all", period: "day", metric: m, metricLabel: METRIC_LABEL[m],
      updatedAt: _cache.updatedAt,
      stockCount: kept.length, boardCount: nodes.length,
      quoteTime: _cache.quoteTime,
      summary: {
        advanceCount: advance, flatCount: flat, declineCount: decline,
        stockCount: kept.length, limitUpCount: limitUp, limitDownCount: limitDown,
        turnoverAmount: sumField(kept, "amount"),
        indexChangePct: round(weightedChange(kept, "totalMcap"), 3),
        countsSource: "tencent", turnoverSource: "tencent",
        indexes: _cache.indexes,
      },
      nodes: nodes,
    };
  }


  /* ============================ 日 K 线 ============================
   * 腾讯 fqkline 接口**允许跨域**（实测 fetch 直接返回 200），所以纯静态部署
   * 也能自己拉 K 线画图，不需要后端。新浪的同类接口被 CORS 挡着，不要用。
   *
   * 返回行格式：[日期, 开, 收, 高, 低, 量]（腾讯的前复权格式就是这个顺序）。
   * ================================================================ */

  // 腾讯这个接口有两个域名，行为不一样（实测）：
  //   ifzq.gtimg.cn       200 / 前复权 qfqday  ✅
  //   web.ifzq.gtimg.cn   501 ❌（原来用的就是这个，被关了）
  // 两个都留着，一个挂了自动换另一个。
  var KLINE_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn"];
  var KLINE_PATH = "/appstock/app/fqkline/get?param=";
  var KLINE_TIMEOUT_MS = 8000;
  var _klineCache = {};
  var _klinePending = {};   // 同一只在飞的请求：并起来，别发两次

  function fetchKline(marketCode, days) {
    var n = days || 120;
    var key = marketCode + ":" + n;
    if (_klineCache[key]) return Promise.resolve(_klineCache[key]);
    if (_klinePending[key]) return _klinePending[key];
    function attempt(hi) {
      if (hi >= KLINE_HOSTS.length) throw new Error("K 线接口两个域名都不可用");
      return fetchOneKline(KLINE_HOSTS[hi], marketCode, n).catch(function (e) {
        if (e && e.message === "EMPTY") throw e;   // 没数据是数据问题，换域名也没用
        return attempt(hi + 1);
      });
    }
    _klinePending[key] = attempt(0)
      .then(function (out) {
        _klineCache[key] = out;
        delete _klinePending[key];
        return out;
      })
      .catch(function (e) {
        delete _klinePending[key];
        throw e;
      });
    return _klinePending[key];
  }

  /** 请求单个域名的日 K。带 8 秒超时，避免请求挂住时面板一直显示「加载中」。 */
  function fetchOneKline(host, marketCode, n) {
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, KLINE_TIMEOUT_MS);
    return fetch(host + KLINE_PATH + marketCode + ",day,,," + n + ",qfq",
                 ctrl ? { signal: ctrl.signal } : undefined)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (d && d.code !== 0) throw new Error("接口 code=" + d.code);
        var node = d && d.data && d.data[marketCode];
        var rows = (node && (node.qfqday || node.day)) || [];
        if (!rows.length) throw new Error("EMPTY");
        clearTimeout(timer);
        return rows.map(function (r) {
          return { date: r[0], open: +r[1], close: +r[2], high: +r[3], low: +r[4], vol: +r[5] };
        });
      })
      .catch(function (e) { clearTimeout(timer); throw e; });
  }

  global.MarketData = {
    toMarketCode: toMarketCode,
    exchangeOf: exchangeOf,
    fetchQuotes: fetchQuotes,
    fetchIndexes: fetchIndexes,
    loadIndustryMap: loadIndustryMap,
    fetchKline: fetchKline,
    fetchAll: fetchAll,
    buildTreemap: buildTreemap,
    computeSectorStrength: computeSectorStrength,
    fetchStrengthClient: fetchStrengthClient,
  };

})(typeof window !== "undefined" ? window : this);
