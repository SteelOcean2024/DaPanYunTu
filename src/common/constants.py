"""
常量与枚举
==========
集中定义跨模块共用的枚举值，避免各处硬编码字符串。

其中 ``PriceType`` 的值直接对应同花顺 ``xd.cmd()`` 命令里的价格关键字，
改动这里会直接影响下单行为，务必对照官方文档：
``同花顺安装目录\\script\\帮助文档\\xiadan.py``
"""
from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    """买卖方向。取值即同花顺命令行的动作词。"""

    BUY = "buy"
    SELL = "sell"


class PriceType(str, Enum):
    """
    委托价格类型。取值是同花顺 ``xd.cmd()`` 的价格关键字简拼。

    用法示例::

        xd.cmd('buy 600000 zxjg 100')   # 最新价
        xd.cmd('sell 600000 dtjg -zcw 0')  # 跌停价清仓
    """

    LATEST = "zxjg"       # 最新价
    LIMIT_UP = "ztjg"     # 涨停价
    LIMIT_DOWN = "dtjg"   # 跌停价
    BID_1 = "mrj1"        # 买一价
    BID_2 = "mrj2"
    BID_3 = "mrj3"
    BID_4 = "mrj4"
    BID_5 = "mrj5"
    ASK_1 = "mcj1"        # 卖一价
    ASK_2 = "mcj2"
    ASK_3 = "mcj3"
    ASK_4 = "mcj4"
    ASK_5 = "mcj5"
    COUNTER_1 = "dsj1"    # 对手价1（买用卖一，卖用买一）
    COUNTER_2 = "dsj2"
    COUNTER_3 = "dsj3"
    COUNTER_4 = "dsj4"
    COUNTER_5 = "dsj5"


class OrderStatus(str, Enum):
    """
    委托状态。与同花顺 ``OrderStatus`` 枚举一一对应。

    注意区分这几个容易混的：

    - ``ERROR``   委托前失败（命令在本地就没发出去）
    - ``FAIL``    委托后失败（报给券商后被拒）
    - ``INACTIVE`` 废单
    """

    ERROR = "error"          # 委托前失败（命令本地失败）
    FAIL = "fail"            # 委托后失败（报给券商后失败）
    SUBMITTED = "submitted"  # 委托成功
    DEALING = "dealing"      # 部分成交
    DEALED = "dealed"        # 全部成交
    CANCEL = "cancel"        # 撤单已报但还没撤单成交
    CANCELED = "canceled"    # 已撤
    INACTIVE = "inactive"    # 废单

    @classmethod
    def terminal_states(cls) -> frozenset["OrderStatus"]:
        """终态集合：到达这些状态后不会再变化，可以停止等待。"""
        return frozenset({cls.DEALED, cls.CANCELED, cls.INACTIVE, cls.ERROR, cls.FAIL})


class Action(str, Enum):
    """决策动作。比 Side 多一个 HOLD，用于表达「看过了但不动」。"""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# ---- 交易规则常量（A 股）----

#: 一手 = 100 股。买入必须是 100 的整数倍（卖出可以不足一手，比如零股）
LOT_SIZE = 100

#: 单笔申报数量上限（股）。超过要拆单
MAX_ORDER_QTY = 1_000_000

#: 印花税（仅卖出收取，2023-08 起减半）
STAMP_DUTY_RATE = 0.0005

#: 过户费（沪深双向收取，2022-04 起）
TRANSFER_FEE_RATE = 0.00001

#: 最低佣金（元），多数券商 5 元
MIN_COMMISSION = 5.0
