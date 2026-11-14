# -*- coding: utf-8 -*-
"""导出四类标的清单 + 指数成分 -> web/data/universe.json / index-members.json

为什么按「代码段枚举」而不是找成分股列表接口：
  1. 腾讯行情接口对**无效代码**回 v_pv_none_match，对有效代码回 v_<code>="..."，
     按段批量问一遍就能拿到真实可交易的清单；
  2. 第三方（东财等）的列表接口在浏览器里被 CORS 挡着，纯静态站用不了；
     而这个是线上行情用的同一个接口，同源、不多一个依赖。
  3. 字段 [61] 直接给了类型标记（ETF / ZQ-KZZ），分类不用猜名字。
"""
import json, re, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "data"
QT = "https://qt.gtimg.cn/q="
BOARD = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code=%s&sort_type=price&direct=down&offset=%d&count=%d"
BATCH = 600

# (前缀, 起, 止, 代码位数) —— 港股是 **5 位**（hk00700），沪深 6 位，别写错。
RANGES = {
    "etf":  [("sh", 510000, 519999, 6), ("sh", 560000, 569999, 6), ("sh", 580000, 589999, 6),
             ("sz", 159000, 159999, 6), ("sz", 160000, 169999, 6)],
    "bond": [("sh", 110000, 119999, 6), ("sz", 120000, 129999, 6)],
    "hk":   [("hk", 1, 9999, 5)],
}
SUFFIX = {"sh": "SH", "sz": "SZ", "hk": "HK"}
LINE = re.compile(r'v_([a-z]{2}\d{5,6})="([^"]*)"')


def get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("gbk", "replace")
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def scan(kind):
    rows, seen = [], set()
    for prefix, lo, hi, w in RANGES[kind]:
        codes = ["%s%0*d" % (prefix, w, n) for n in range(lo, hi + 1)]
        for i in range(0, len(codes), BATCH):
            chunk = codes[i:i + BATCH]
            try:
                text = get(QT + ",".join(chunk))
            except Exception as e:
                print("  跳过一段 %s: %s" % (chunk[0], e))
                continue
            for mc, body in LINE.findall(text):
                if mc in seen:
                    continue
                seen.add(mc)
                f = body.split("~")
                if len(f) < 4:
                    continue
                name = f[1].strip()
                typ = f[61].strip() if len(f) > 61 else ""
                if not name or f[3] in ("", "0.000"):
                    continue
                rows.append({"c": mc[2:] + "." + SUFFIX[mc[:2]], "n": name, "t": typ})
        print("  %s %s%0*d-%0*d 累计 %d" % (kind, prefix, w, lo, w, hi, len(rows)))
    return rows


def board(code, want):
    """指数成分。这个接口时通时不通，多试几次；拿不到就返回 None。"""
    out, offset = [], 0
    while offset < want:
        try:
            text = get(BOARD % (code, offset, 200), tries=6)
            d = json.loads(text)
            lst = (d.get("data") or {}).get("rank_list") or []
        except Exception as e:
            print("  指数 %s 取不到: %s" % (code, e))
            return None
        if not lst:
            break
        for it in lst:
            out.append(it["code"])
        offset += len(lst)
        if len(lst) < 200:
            break
    return out or None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    uni = {}
    for kind in ("etf", "bond", "hk"):
        print("[%s] 枚举中…" % kind)
        uni[kind] = scan(kind)
        print("[%s] %d 只" % (kind, len(uni[kind])))
    (OUT / "universe.json").write_text(
        json.dumps(uni, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("写出 universe.json")

    idx = {}
    for name, code, want in (("hs300", "sh000300", 300), ("a500", "sh000510", 500)):
        got = board(code, want)
        if got:
            idx[name] = got
            print("[%s] %d 只" % (name, len(got)))
        else:
            print("[%s] 这次没取到" % name)
    if idx:
        (OUT / "index-members.json").write_text(
            json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print("写出 index-members.json")
    else:
        print("指数成分一个都没取到，跳过（前端会按「无缓存」处理）")


if __name__ == "__main__":
    main()
