# -*- coding: utf-8 -*-
"""
大盘云图数据层测试
==================
只测**纯逻辑**（组装、加权、行业挂载、代码判定），不碰网络。

为什么刻意不测网络
------------------
行情接口是外部依赖，测它会引入不确定性（超时、限流、非交易时段无数据），
让 CI 变成随机红灯。网络层靠 `scripts/heatmap_server.py` 跑通来验证，
这里只保证「给定输入，输出一定正确」的那部分。
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from src.data.codes import exchange_of, to_market_code
from src.heatmap import industry as industry_mod
from src.heatmap import treemap as treemap_mod


# ============================================================
# 构造测试用行情表
# ============================================================


def make_quote(
    code: str,
    name: str,
    exchange: str,
    price: float,
    change_pct: float,
    total_mcap: float,
    amount: float = 1e8,
    l1_name: str = "电子",
    l1_code: str = "801080.SI",
    l2_name: str = "半导体",
    l2_code: str = "801081.SI",
    limit_up: float = 0.0,
    limit_down: float = 0.0,
) -> dict:
    """造一条行情记录，字段与 `fetch_quotes` 的输出对齐。"""
    return {
        "market_code": f"{exchange.lower()}{code}",
        "code": code,
        "name": name,
        "exchange": exchange,
        "price": price,
        "prev_close": price,
        "open": price,
        "high": price,
        "low": price,
        "change": 0.0,
        "change_pct": change_pct,
        "volume": 1e6,
        "amount": amount,
        "turnover_rate": 1.0,
        "float_mcap": total_mcap,
        "total_mcap": total_mcap,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "quote_time": "20261112150000",
        "l1_code": l1_code,
        "l1_name": l1_name,
        "l2_code": l2_code,
        "l2_name": l2_name,
    }


@pytest.fixture()
def sample() -> pd.DataFrame:
    """两只电子股（一涨一跌）+ 一只银行股，市值差别明显。"""
    return pd.DataFrame(
        [
            make_quote("688256", "寒武纪", "SH", 100.0, 10.0, 6.0e11),
            make_quote("601138", "工业富联", "SH", 43.0, -5.0, 8.6e11),
            make_quote(
                "601398", "工商银行", "SZ", 8.69, 2.0, 3.0e12,
                l1_name="银行", l1_code="801780.SI",
                l2_name="国有大型银行Ⅱ", l2_code="801782.SI",
            ),
        ]
    )


# ============================================================
# 代码 / 交易所判定（云图踩过坑的地方）
# ============================================================


class TestExchange:
    """交易所判定：指数、北交所新号段、B 股都踩过坑。"""

    def test_常规前缀(self):
        assert exchange_of("600000") == "sh"
        assert exchange_of("688256") == "sh"
        assert exchange_of("000001") == "sz"
        assert exchange_of("300750") == "sz"

    def test_北交所老号段(self):
        assert exchange_of("430047") == "bj"
        assert exchange_of("831010") == "bj"

    def test_北交所新号段_92(self):
        """920xxx 是 2024 年启用的北交所新号段，早期代码漏判导致整批被丢弃。"""
        assert exchange_of("920002") == "bj"
        assert to_market_code("920002") == "bj920002"

    def test_上海B股不是北交所(self):
        """900xxx 是上海 B 股。不能把 9 开头一律当北交所。"""
        assert exchange_of("900901") == "sh"

    def test_指数代码不适用本函数(self):
        """
        深证成指 399001 反推不出来——这是**有意为之**。
        指数的交易所只能从数据源的市场前缀判断，
        所以行情解析里直接取 `v_sz399001` 的前缀，而不是调用本函数。
        """
        with pytest.raises(ValueError):
            exchange_of("399001")


# ============================================================
# 加权涨跌幅
# ============================================================


class TestWeightedChange:
    def test_按市值加权而不是算术平均(self):
        df = pd.DataFrame(
            {
                "value": [9.0e11, 1.0e11],
                "change_pct": [10.0, 0.0],
            }
        )
        # 算术平均是 5.0；按市值加权应为 10*0.9 + 0*0.1 = 9.0
        assert industry_mod is not None  # 保持 import 有意义
        got = treemap_mod._weighted_change(df, "value")
        assert got == pytest.approx(9.0)
        assert got != pytest.approx(5.0)

    def test_权重全为零时回落算术平均(self):
        df = pd.DataFrame({"value": [0.0, 0.0], "change_pct": [4.0, -2.0]})
        assert treemap_mod._weighted_change(df, "value") == pytest.approx(1.0)

    def test_空表不炸(self):
        df = pd.DataFrame({"value": [], "change_pct": []})
        assert treemap_mod._weighted_change(df, "value") == 0.0


# ============================================================
# 汇总
# ============================================================


class TestSummary:
    def test_涨跌家数(self):
        df = pd.DataFrame(
            {
                "change_pct": [1.0, -2.0, 0.0, 3.0, -1.0],
                "amount": [1e8] * 5,
                "total_mcap": [1e10] * 5,
                "price": [10.0] * 5,
                "limit_up": [0.0] * 5,
                "limit_down": [0.0] * 5,
            }
        )
        s = treemap_mod.build_summary(df)
        assert s["advanceCount"] == 2
        assert s["declineCount"] == 2
        assert s["flatCount"] == 1
        assert s["stockCount"] == 5
        assert s["turnoverAmount"] == pytest.approx(5e8)

    def test_涨停跌停按触及涨跌停价统计(self):
        df = pd.DataFrame(
            {
                "change_pct": [10.0, -10.0, 1.0],
                "amount": [1e8] * 3,
                "total_mcap": [1e10] * 3,
                "price": [11.0, 9.0, 10.0],
                "limit_up": [11.0, 11.0, 11.0],
                "limit_down": [9.0, 9.0, 9.0],
            }
        )
        s = treemap_mod.build_summary(df)
        assert s["limitUpCount"] == 1
        assert s["limitDownCount"] == 1

    def test_没有涨跌停价时不计入(self):
        """指数类记录 limit_up/limit_down 为 0，不能算成涨停。"""
        df = pd.DataFrame(
            {
                "change_pct": [1.0],
                "amount": [1e8],
                "total_mcap": [1e10],
                "price": [10.0],
                "limit_up": [0.0],
                "limit_down": [0.0],
            }
        )
        s = treemap_mod.build_summary(df)
        assert s["limitUpCount"] == 0
        assert s["limitDownCount"] == 0


# ============================================================
# 云图组装
# ============================================================


class TestBuildTreemap:
    def test_三级结构(self, sample):
        tree = treemap_mod.build_treemap(sample)
        assert tree["stockCount"] == 3
        assert tree["boardCount"] == 2  # 电子、银行

        elec = next(n for n in tree["nodes"] if n["name"] == "电子")
        assert elec["stockCount"] == 2
        assert len(elec["children"]) == 1  # 只有一个二级行业
        assert elec["children"][0]["name"] == "半导体"
        assert len(elec["children"][0]["children"]) == 2  # 两只个股

        leaf = elec["children"][0]["children"][0]
        assert leaf["code"].endswith((".SH", ".SZ"))
        assert leaf["boardName"] == "电子"
        assert leaf["subBoardName"] == "半导体"

    def test_板块按面积降序(self, sample):
        tree = treemap_mod.build_treemap(sample)
        values = [n["value"] for n in tree["nodes"]]
        assert values == sorted(values, reverse=True)
        # 银行 3 万亿 > 电子 1.46 万亿
        assert tree["nodes"][0]["name"] == "银行"

    def test_二级行业也按面积降序(self):
        df = pd.DataFrame(
            [
                make_quote("000001", "小票", "SZ", 10.0, 1.0, 1e9,
                           l2_name="小行业", l2_code="801002.SI"),
                make_quote("000002", "大票", "SZ", 10.0, 1.0, 9e11,
                           l2_name="大行业", l2_code="801001.SI"),
            ]
        )
        tree = treemap_mod.build_treemap(df)
        subs = tree["nodes"][0]["children"]
        assert [s["name"] for s in subs] == ["大行业", "小行业"]

    def test_个股按面积降序(self, sample):
        elec = next(n for n in treemap_mod.build_treemap(sample)["nodes"]
                    if n["name"] == "电子")
        stocks = elec["children"][0]["children"]
        assert [s["name"] for s in stocks] == ["工业富联", "寒武纪"]

    def test_面积口径切换(self, sample):
        for metric in treemap_mod.METRICS:
            tree = treemap_mod.build_treemap(sample, metric=metric)
            assert tree["metric"] == metric
            assert tree["metricLabel"] == treemap_mod.METRICS[metric]
            assert tree["nodes"][0]["value"] > 0

    def test_未知口径报错(self, sample):
        with pytest.raises(ValueError, match="未知的面积口径"):
            treemap_mod.build_treemap(sample, metric="pe_ratio")

    def test_空表报错(self):
        with pytest.raises(ValueError, match="行情表为空"):
            treemap_mod.build_treemap(pd.DataFrame())

    def test_面积为零的个股被剔除(self, sample):
        """停牌股成交额为 0 会让方块退化成零宽矩形，必须剔除。"""
        df = pd.concat(
            [
                sample,
                pd.DataFrame([make_quote("600000", "停牌股", "SH", 10.0, 0.0, 0.0)]),
            ],
            ignore_index=True,
        )
        tree = treemap_mod.build_treemap(df, metric="total_mcap")
        codes = [
            s["name"]
            for n in tree["nodes"]
            for sub in n["children"]
            for s in sub["children"]
        ]
        assert "停牌股" not in codes
        assert tree["stockCount"] == 3

    def test_汇总里带指数(self, sample):
        idx = {"上证指数": {"price": 3655.83, "change_pct": -0.67}}
        tree = treemap_mod.build_treemap(sample, index_quotes=idx)
        assert tree["summary"]["indexes"]["上证指数"]["price"] == pytest.approx(3655.83)


# ============================================================
# 行业挂载
# ============================================================


class TestAttachIndustry:
    def test_匹配到的挂上行业(self):
        quotes = pd.DataFrame([{"code": "688256", "name": "寒武纪"}])
        mapping = pd.DataFrame(
            [{"code": "688256", "name": "寒武纪", "l1_code": "801080.SI",
              "l1_name": "电子", "l2_code": "801081.SI", "l2_name": "半导体"}]
        )
        out = industry_mod.attach_industry(quotes, mapping)
        assert out.loc[0, "l1_name"] == "电子"
        assert out.loc[0, "l2_name"] == "半导体"

    def test_匹配不到的归入未分类而不是丢弃(self):
        """
        丢弃会让云图个股数对不上大盘，用户会以为数据缺了；
        归入「未分类」则一眼能看出是哪几只没归上。
        """
        quotes = pd.DataFrame([{"code": "999999", "name": "次新股"}])
        mapping = pd.DataFrame(
            columns=["code", "name", "l1_code", "l1_name", "l2_code", "l2_name"]
        )
        out = industry_mod.attach_industry(quotes, mapping)
        assert len(out) == 1
        assert out.loc[0, "l1_name"] == industry_mod.UNCLASSIFIED[1]
        assert out.loc[0, "l2_name"] == industry_mod.UNCLASSIFIED[3]

    def test_不丢行(self):
        quotes = pd.DataFrame(
            [{"code": "688256", "name": "A"}, {"code": "999999", "name": "B"}]
        )
        mapping = pd.DataFrame(
            [{"code": "688256", "name": "A", "l1_code": "1", "l1_name": "电子",
              "l2_code": "2", "l2_name": "半导体"}]
        )
        out = industry_mod.attach_industry(quotes, mapping)
        assert len(out) == 2


# ============================================================
# 腾讯字段解析
# ============================================================


class TestQuoteParsing:
    """解析是纯函数，可以离线测——用真实报文片段。"""

    RAW = (
        'v_sh600000="1~浦发银行~600000~10.21~10.17~10.14~500817~228508~272309~10.20~105~'
        '10.19~116~10.18~852~10.17~155~10.16~359~10.21~349~10.22~1761~10.23~4320~10.24~3217~'
        '10.25~2875~~20261112153031~0.04~0.39~10.24~10.03~10.21/500817/508447361~500817~50845~'
        '0.15~6.57~~10.24~10.03~2.06~3400.53~3400.53~0.45~11.19~9.15~0.91~-10935~10.15~6.29~'
        '6.80~~~~-0.01~50844.7361~3.6756~36~   A~GP-A~-15.06~3.44~4.11~6.17~0.50~12.06~8.07~'
        '2.20~5.91~11.34~33305838300~33305838300~-77.50~4.61~33305838300~~~~-9.33~0.10~~CNY~0~'
        '___D__F__N~10.28~-9011~"'
    )

    def test_解析真实报文(self):
        from src.heatmap.quotes import _parse_line

        row = _parse_line(self.RAW)
        assert row is not None
        assert row["code"] == "600000"
        assert row["name"] == "浦发银行"
        assert row["exchange"] == "SH"
        assert row["price"] == pytest.approx(10.21)
        assert row["change_pct"] == pytest.approx(0.39)

    def test_单位换算(self):
        """
        腾讯字段单位极易踩坑：
        [37] 成交额是**万元**、[44]/[45] 市值是**亿元**、[36] 成交量是**手**。
        不换算云图面积会差好几个数量级。
        """
        from src.heatmap.quotes import _parse_line

        row = _parse_line(self.RAW)
        assert row["amount"] == pytest.approx(50845 * 1e4)          # 万元 → 元
        assert row["total_mcap"] == pytest.approx(3400.53 * 1e8)    # 亿元 → 元
        assert row["float_mcap"] == pytest.approx(3400.53 * 1e8)
        assert row["volume"] == pytest.approx(500817 * 100)         # 手 → 股

    def test_指数用市场前缀判定交易所(self):
        """
        深证成指 399001 用代码反推会抛异常，必须取报文里的 `sz` 前缀。
        """
        from src.heatmap.quotes import _parse_line

        raw = self.RAW.replace("v_sh600000", "v_sz399001").replace(
            "~浦发银行~600000~", "~深证成指~399001~"
        )
        row = _parse_line(raw)
        assert row is not None
        assert row["exchange"] == "SZ"
        assert row["market_code"] == "sz399001"

    def test_停牌股返回None(self):
        from src.heatmap.quotes import _parse_line

        raw = self.RAW.replace("~10.21~10.17~", "~0.00~10.17~")
        assert _parse_line(raw) is None

    def test_垃圾输入不抛异常(self):
        from src.heatmap.quotes import _parse_line

        for bad in ["", "garbage", 'v_sh600000="";', "v_sh600000=\"1~2~\";"]:
            assert _parse_line(bad) is None

    def test_北交所前缀(self):
        from src.heatmap.quotes import _parse_line

        raw = self.RAW.replace("v_sh600000", "v_bj920002")
        row = _parse_line(raw)
        assert row is not None
        assert row["exchange"] == "BJ"

    def test_涨跌幅为零不算NaN(self):
        from src.heatmap.quotes import _parse_line

        row = _parse_line(self.RAW)
        assert not math.isnan(row["change_pct"])
