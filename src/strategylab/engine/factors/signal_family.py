# -*- coding: utf-8 -*-
"""信号族分类（移植自 deepseek-harness-quant factors/signal_family.py，MIT）。

信号族（按因子信号本质归类，9 族 + 其他兜底）：
  价值 / 成长 / 质量 / 量价 / 情绪 / 反转动量 / 资金 / 政策 / 其他

用法：
  from strategylab.engine.factors import signal_family_of, SIGNAL_FAMILY_CN, SIGNAL_FAMILY_ORDER
  signal_family_of("sue")        # → "成长"
  signal_family_of("unknown_x")  # → "其他"
"""
from __future__ import annotations

# 因子名（去 _rank 后缀）→ 信号族
SIGNAL_FAMILY = {
    # ── 价值信号（低估值/股息）
    "value": "价值", "bp": "价值", "pe": "价值", "pb": "价值",
    "pe_pct": "价值", "pb_pct": "价值", "div_yield": "价值", "pcf": "价值",
    # ── 成长信号（单季增速/SUE/加速）
    "growth": "成长", "sue": "成长", "yoy_accel": "成长", "sq_nyoy": "成长",
    "pead": "成长", "asset_growth": "成长", "inst_surv": "成长",
    "gross_margin_chg": "成长", "o2c": "成长",
    # ── 质量信号（ROE/负债/现金流/盈余质量）
    "quality": "质量", "roe": "质量", "roa": "质量", "liability": "质量",
    "cfo_health": "质量", "accruals": "质量", "f_score": "质量", "gp_a": "质量",
    # ── 量价信号（换手/量比/突破/量价结构）
    "turnover": "量价", "vol_ratio": "量价", "vol_contract": "量价",
    "turn_mid_prox": "量价", "turn60": "量价", "turn_std20": "量价",
    "amihud": "量价", "obv_break": "量价", "near_high": "量价",
    "near_high_250": "量价", "near_ma250": "量价", "ma_trend": "量价",
    "ma50_up": "量价", "ma200_up": "量价", "below_ma120": "量价",
    "std20": "量价", "amp20": "量价",
    # ── 情绪信号（涨停/连板/炸板/资金流入/散户热度）
    "sentiment": "情绪", "limit_up_flag": "情绪", "limit_down_flag": "情绪",
    "consec_limit_up": "情绪", "consec_limit_down": "情绪",
    "limit_up_premium": "情绪", "limit_up_next_ret": "情绪",
    "limit_up_turn": "情绪", "limit_up_vol_ratio": "情绪",
    "limit_up_cnt_5d": "情绪", "open_limit_up_fail": "情绪",
    "limit_down_next_ret": "情绪", "ind_moneyflow": "情绪", "sector_break": "情绪",
    "open_prem_20": "情绪",        # 开盘溢价
    "limup_ex_ret_20": "情绪",     # 涨停次日超额
    "o2c_sum_20": "反转动量",      # 日内收益反转
    "lhb_jg_cnt_20": "资金",       # 龙虎榜机构次数
    "ind_rs_20": "反转动量",       # 行业相对强弱反转
    "ind_crowd_60": "量价",        # 行业拥挤度
    "open_gap": "情绪",            # 开盘缺口
    "close_to_high": "反转动量",   # 收盘相对高点
    "open_vol_share": "流动性",    # 开盘量占比
    "tail_vol_gain": "情绪",       # 尾盘放量拉升=诱多
    "intraday_range": "量价",      # 日内振幅
    "am_pm_ratio": "量价",         # 上午/下午量比
    "close30_ret": "量价", "tail60_ret": "量价", "pm_ret": "量价",
    "head60_ret": "量价", "first_half_ret": "量价", "tail_vol_share": "量价",
    "vshape": "量价", "open30_ret": "量价",
    "kline_hammer_cnt": "质量",   # 锤子线
    "kline_doji_cnt": "质量",     # 十字星
    "kline_shooting_cnt": "质量",  # 射击之星
    "kline_bear_streak": "反转动量",  # 连阴线
    "kline_5m_std": "量价",       # 5分钟波动
    "kline_ret_skew": "量价", "kline_mom_ratio": "量价", "kline_last_5m": "量价",
    # ── 反转/动量信号（超跌反弹/动量/低波防守）
    "reversal20": "反转动量", "reversal5": "反转动量", "max_ret20": "反转动量",
    "drawdown60": "反转动量", "drawdown_60d": "反转动量", "rsi14": "反转动量",
    "skew20": "反转动量", "kurt20": "反转动量", "downside_vol": "反转动量",
    "mom120": "反转动量", "mom60": "反转动量", "lowvol": "反转动量",
    # factor_engine 命名（带下划线）与既有 mom120/mom60 对齐
    "mom_20": "反转动量", "mom_120": "反转动量", "lowvol_60": "反转动量",
    "rps_120": "反转动量", "near_high_250": "量价", "new_high_250": "量价",
    # ── 资金信号（社保/机构持仓）
    "shebao_hold": "资金", "shebao_chg": "资金", "shebao_chg_pct": "资金",
    "inst_hold_chg": "资金",
    # ── 政策信号（EPU/政策事件）
    "epu": "政策", "epu_level": "政策", "epu_z12": "政策", "policy": "政策",
    # ── 其他兜底（Alpha/规模/无法归类）
    "size": "其他", "alpha003": "其他", "alpha004": "其他", "alpha006": "其他",
    "alpha015": "其他", "alpha044": "其他", "alpha050": "其他", "alpha101": "其他",
    "res_alpha21": "其他", "rmax": "其他", "non_st": "其他", "inst_hold": "其他",
}

SIGNAL_FAMILY_CN = {
    "价值": "价值信号", "成长": "成长信号", "质量": "质量信号",
    "量价": "量价信号", "情绪": "情绪信号", "反转动量": "反转/动量信号",
    "资金": "资金信号", "政策": "政策信号", "其他": "其他",
}

# 展示顺序（其他最后）
SIGNAL_FAMILY_ORDER = ["价值", "成长", "质量", "量价", "情绪", "反转动量", "资金", "政策", "其他"]

# 族色（UI 徽章）
SIGNAL_FAMILY_COLOR = {
    "价值": "#0F6E56", "成长": "#2F6FED", "质量": "#0E8A7E", "量价": "#D4A843",
    "情绪": "#C0392B", "反转动量": "#7A5FD0", "资金": "#B07CC6", "政策": "#185FA5",
    "其他": "#8a94a6",
}


def signal_family_of(factor: str) -> str:
    """因子名（带或不带 _rank 后缀）→ 信号族；无法归类 → '其他'"""
    if not factor:
        return "其他"
    f = factor.lower()
    if f.endswith("_rank"):
        f = f[:-5]
    # 直接命中
    if f in SIGNAL_FAMILY:
        return SIGNAL_FAMILY[f]
    # 后缀匹配（如 f_score、o2c_sum_20 → o2c）
    for key, fam in SIGNAL_FAMILY.items():
        if f.startswith(key) or key.startswith(f):
            return fam
    return "其他"


# 主系统财务因子 → 外包因子池信号因子 别名映射（机会池×因子池联动兜底）
FACTOR_ALIAS = {
    # 成长（→ SUE/加速）
    "sq_nyoy": "sue",            # 单季净利同比 → SUE
    "gross_margin_chg": "sue",   # 毛利率变化 → 成长代理
    "inst_surv": "sue",          # 机构调研强度 → 成长代理
    "inst_hold_chg": "shebao_chg",  # 机构持仓变化 → 资金
    # 估值（→ bp）
    "pe_pct": "bp", "pb_pct": "bp", "div_yield": "bp",
    # 质量（→ f_score/quality）
    "roe": "f_score", "liability": "f_score", "cfo_health": "f_score",
    # 量价（→ 面板因子与外包同名/近义）
    "drawdown_60d": "drawdown60",
    "near_high_250": "near_high",
    "ma50_up": "ma_trend", "ma200_up": "ma_trend",
    "ind_moneyflow": "sentiment",   # 资金流 → 情绪代理
}


def alias_of(factor: str) -> str:
    """因子名 → 外包别名（无则原样返回）"""
    f = factor.lower()
    if f.endswith("_rank"):
        f = f[:-5]
    return FACTOR_ALIAS.get(f, f)


# manifest category → 信号族：新因子在 manifest 登记类别即自动归族
CATEGORY_TO_FAMILY = {
    # 中文（历史兼容）
    "估值": "价值", "基本面": "成长", "量价": "量价", "情绪": "情绪",
    "政策": "政策", "主观": "其他", "风险": "其他", "成长": "成长",
    "价值": "价值", "资金": "资金", "动量": "反转动量", "反转": "反转动量",
    "质量": "质量",
    # 英文（外包 manifest 实际类别）
    "short_term": "情绪", "emotion": "情绪", "event": "资金",
    "kline": "量价", "chip": "筹码", "industry": "行业",
    "institution": "资金", "leverage": "资金", "a_share_alpha": "反转动量",
    "alpha101": "量价", "a_alpha": "反转动量", "smart_beta": "反转动量",
    "fundamental_lowfreq": "质量",
}


def category_to_family(category: str) -> str:
    """manifest 类别 → 信号族；无法映射 → ''（调用方兜底'其他'）"""
    if not category:
        return ""
    for k, v in CATEGORY_TO_FAMILY.items():
        if k in category or category in k:
            return v
    return ""
