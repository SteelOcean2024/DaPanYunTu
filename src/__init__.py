"""
同花顺自动化量化交易系统 —— 外部大脑。

模块划分（按数据流向）：

    common/     公共基础设施：路径、配置、日志、常量
    data/       数据层：数据模型、行情获取、本地存储
    analysis/   分析层：技术指标、信号计算
    decision/   决策层：大模型客户端、提示词、决策引擎
    risk/       风控层：规则、守门器
    execution/  执行层：文件协议、与同花顺桥接

数据流：

    data → analysis → decision → risk → execution
"""

__version__ = "0.1.0"
