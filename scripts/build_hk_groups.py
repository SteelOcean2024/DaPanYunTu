# -*- coding: utf-8 -*-
"""给 universe.json 的港股补上行业（字段 g）。

港股和可转债一样没有行业分类。两层映射：
  1) 名称去后缀（-W / -S / -T / -B、股份 / 集团 / 控股）后与 A 股名匹配，
     命中就用该 A 股的申万一级 —— A+H 两地上市的大票基本都能命中
     （宁德时代 / 比亚迪 / 美的集团 / 中国神华 / 中国平安 / 药明康德 …）。
  2) 没命中的走港股专有名单（腾讯 / 阿里 / 汇丰 / 中银香港 这些 A 股没有）。
"""
import json, re, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "data"
QT = "https://qt.gtimg.cn/q="
BATCH = 600
LINE = re.compile(r'v_([a-z]{2}\d{5,6})="([^"]*)"')

HK_EXTRA = [
    # 名字必须用**申万一级**的口径，才能和按 A 股名匹配上的那一批混在同一张图里。
    ("银行",     r"银行|汇丰|渣打|恒生银行|中银香港|东亚银行"),
    ("非银金融", r"保险|友邦|保诚|中国太平|众安|证券|金融|交易所|港交所|中信|招商|光大|国泰君安|海通"),
    ("房地产",   r"地产|置业|新鸿基|长实|恒基|新世界|九龙仓|领展|太古"),
    ("煤炭",     r"神华|煤|兖矿"),
    ("石油石化", r"石油|石化"),
    ("有色金属", r"矿业|紫金|中国铝业|洛阳钼业"),
    ("医药生物", r"医药|药明|石药|中生制药|翰森|百济|信达|康希诺|威高|国药"),
    ("电子",     r"中芯国际|华虹|舜宇|瑞声|半导体"),
    ("计算机",   r"腾讯|阿里巴巴|美团|京东|快手|网易|百度|小米|携程|哔哩|商汤|金蝶|金山|联想|阅文|平安好医生"),
    ("食品饮料", r"农夫山泉|蒙牛|啤酒|康师傅|统一|飞鹤|华润饮料|颐海"),
    ("纺织服饰", r"安踏|李宁|周大福|申洲|万洲|服装"),
    ("社会服务", r"海底捞|餐饮|旅游"),
    ("通信",     r"移动|联通|电信"),
    ("公用事业", r"电力|燃气|水务|港铁|长江基建|中电控股|华润电力|中国燃气|粤海"),
    ("汽车",     r"汽车|比亚迪|吉利|长城|小鹏|理想|蔚来|广汽|东风"),
]
HK_EXTRA = [(n, re.compile(p)) for n, p in HK_EXTRA]


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


def stock_names(codes):
    out, mcs = {}, []
    for c in codes:
        p = c.split(".")
        if len(p) == 2:
            mcs.append(p[1].lower() + p[0])
    for i in range(0, len(mcs), BATCH):
        try:
            text = get(QT + ",".join(mcs[i:i + BATCH]))
        except Exception:
            continue
        for mc, body in LINE.findall(text):
            f = body.split("~")
            if len(f) > 2 and f[1].strip():
                out[mc[2:]] = f[1].strip()
    return out


def norm(nm):
    return re.sub(r"(-W|-S|-T|-B|-R|股份|集团|控股|国际)$", "", nm).strip()


def main():
    imap = json.loads((DATA / "industry-map.json").read_text(encoding="utf-8"))
    uni = json.loads((DATA / "universe.json").read_text(encoding="utf-8"))
    l1 = imap["l1"]
    codes = ["%s.%s" % (s[0], "SH" if s[0][0] == "6" else ("SZ" if s[0][0] in "03" else "BJ"))
             for s in imap["stocks"]]
    print("取 A 股名称", len(codes))
    names = stock_names(codes)
    code_l1 = {s[0]: l1[s[1]] for s in imap["stocks"]}
    name_l1 = {nm: code_l1[c] for c, nm in names.items() if c in code_l1}
    print("名称->行业", len(name_l1))

    exact = dict(name_l1)
    nrm = {}
    for nm, g in name_l1.items():
        nrm.setdefault(norm(nm), g)
    by2 = {}
    for nm, g in name_l1.items():
        by2.setdefault(nm[:2], []).append((nm, g))

    hit_a = hit_k = miss = 0
    for x in uni["hk"]:
        nm = x["n"]
        g = exact.get(nm) or nrm.get(norm(nm)) or ""
        if not g:
            base = norm(nm)
            for k in range(min(len(base), 8), 1, -1):
                for cand, gg in by2.get(base[:2], []):
                    if cand.startswith(base[:k]):
                        g = gg
                        break
                if g:
                    break
        if g:
            hit_a += 1
        else:
            for gn, rx in HK_EXTRA:
                if rx.search(nm):
                    g = gn
                    break
            if g:
                hit_k += 1
            else:
                miss += 1
        x["g"] = g
    print("按 A 股名匹配", hit_a, "| 港股专有名单", hit_k, "| 未匹配", miss)
    (DATA / "universe.json").write_text(
        json.dumps(uni, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("写出 universe.json")


if __name__ == "__main__":
    main()
