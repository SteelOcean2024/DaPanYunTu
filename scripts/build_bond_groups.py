# -*- coding: utf-8 -*-
"""给 universe.json 的可转债补上正股所属的申万一级行业（字段 g）。

可转债自己没有行业分类。做法：
  转债简称去掉「转债 / 转 / EB」→ 与 A 股名称做前缀匹配 → 取该股的申万一级。
  例：浦发转债 → 浦发 → 浦发银行 → 银行；盛德转债 → 盛德 → 盛德鑫泰 → 机械设备。

匹配策略：从长到短试前缀，命中即止；实在匹配不上留空，前端归「未匹配」。
"""
import json, re, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "data"
QT = "https://qt.gtimg.cn/q="
BATCH = 600


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


LINE = re.compile(r'v_([a-z]{2}\d{5,6})="([^"]*)"')


def stock_names(codes):
    """code -> 名称。"""
    out = {}
    mcs = []
    for c in codes:
        p = c.split(".")
        if len(p) == 2:
            mcs.append(p[1].lower() + p[0])
    for i in range(0, len(mcs), BATCH):
        chunk = mcs[i:i + BATCH]
        try:
            text = get(QT + ",".join(chunk))
        except Exception as e:
            print("  跳过一段:", e)
            continue
        for mc, body in LINE.findall(text):
            f = body.split("~")
            if len(f) > 2 and f[1].strip():
                out[mc[2:]] = f[1].strip()
        print("  名称 %d/%d" % (min(i + BATCH, len(mcs)), len(mcs)))
    return out


def main():
    imap = json.loads((DATA / "industry-map.json").read_text(encoding="utf-8"))
    uni = json.loads((DATA / "universe.json").read_text(encoding="utf-8"))
    l1 = imap["l1"]

    codes = ["%s.%s" % (s[0], "SH" if s[0][0] == "6" else ("SZ" if s[0][0] in "03" else "BJ"))
             for s in imap["stocks"]]
    print("取 A 股名称，共", len(codes))
    names = stock_names(codes)
    print("拿到名称", len(names))

    code_l1 = {}
    for s in imap["stocks"]:
        code_l1[s[0]] = l1[s[1]]
    name_l1 = {}
    for code, nm in names.items():
        if code in code_l1:
            name_l1[nm] = code_l1[code]
    print("名称->行业", len(name_l1))

    by2 = {}
    for nm, g in name_l1.items():
        by2.setdefault(nm[:2], []).append((nm, g))

    hit = miss = 0
    for b in uni["bond"]:
        base = re.sub(r"(转债|转|EB)$", "", b["n"]).strip()
        g = ""
        for k in range(min(len(base), 8), 1, -1):
            for nm, gg in by2.get(base[:2], []):
                if nm.startswith(base[:k]):
                    g = gg
                    break
            if g:
                break
        b["g"] = g
        if g:
            hit += 1
        else:
            miss += 1
    print("匹配上", hit, "未匹配", miss)
    (DATA / "universe.json").write_text(
        json.dumps(uni, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("写出 universe.json")


if __name__ == "__main__":
    main()
