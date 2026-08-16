# 代码地址 https://github.com/mpquant/MyTT
# 引入说明：从 OSkhQuant 原样吸收，仅补 `import math`（原文件使用 math.* 但漏导入），
# 并置于 strategylab.engine.vendor 命名空间，避免与 engine/indicators.py 中
# 既有的 compute_macd / compute_kdj 口径冲突（两者命名空间独立）。
import math
import numpy as np
import pandas as pd


# ------------------ 0级：核心工具函数（适配日线数据字段） --------------------------------------------
# 日线数据核心字段映射说明：
# - CLOSE: 对应数据中的"收盘价"字段（股票当日收盘价）
# - HIGH: 对应数据中的"最高价"字段（股票当日最高价）
# - LOW: 对应数据中的"最低价"字段（股票当日最低价）
# - OPEN: 对应数据中的"开盘价"字段（股票当日开盘价）
# - VOL: 对应数据中的"成交量(手)"字段（注意单位为手，1手=100股）
# - REF_CLOSE: 对应数据中的"昨收价"字段（前日收盘价），可用REF(CLOSE, 1)替代

def RD(N, D=3):   return np.round(N, D)  # 四舍五入取D位小数


def RET(S, N=1):  return np.array(S)[-N]  # 返回序列倒数第N个值（默认最后一个）


def ABS(S):      return np.abs(S)  # 绝对值


def LN(S):       return np.log(S)  # 自然对数


def POW(S, N):   return np.power(S, N)  # S的N次方


def SQRT(S):     return np.sqrt(S)  # 平方根


def SIN(S):      return np.sin(S)  # 正弦（弧度）


def COS(S):      return np.cos(S)  # 余弦（弧度）


def TAN(S):      return np.tan(S)  # 正切（弧度）


def MAX(S1, S2): return np.maximum(S1, S2)  # 序列最大值


def MIN(S1, S2): return np.minimum(S1, S2)  # 序列最小值


def IF(S, A, B): return np.where(S, A, B)  # 布尔判断（S为真返回A，否则B）


def REF(S, N=1):  # 序列后移N位（获取历史值，如REF(CLOSE,1)为昨收价）
    return pd.Series(S).shift(N).values


def DIFF(S, N=1):  # 序列差分（前值-后值，如DIFF(CLOSE)为当日涨跌额）
    return pd.Series(S).diff(N).values


def STD(S, N):  # N日标准差（如计算波动率）
    return pd.Series(S).rolling(N).std(ddof=0).values


def SUM(S, N):  # N日累计和（N=0为累加，如计算总成交量）
    return pd.Series(S).rolling(N).sum().values if N > 0 else pd.Series(S).cumsum().values


def CONST(S):  # 序列末尾值扩展为等长常量（如固定基准值）
    return np.full(len(S), S[-1])


def HHV(S, N):  # N日最高价（如HHV(HIGH, 5)为最近5日最高价）
    return pd.Series(S).rolling(N).max().values


def LLV(S, N):  # N日最低价（如LLV(LOW, 5)为最近5日最低价）
    return pd.Series(S).rolling(N).min().values


def HHVBARS(S, N):  # N日内最高价到当前的周期数（如找最近5日高点位置）
    return pd.Series(S).rolling(N).apply(lambda x: np.argmax(x[::-1]), raw=True).values


def LLVBARS(S, N):  # N日内最低价到当前的周期数（如找最近5日低点位置）
    return pd.Series(S).rolling(N).apply(lambda x: np.argmin(x[::-1]), raw=True).values


def MA(S, N):  # N日简单移动平均（如MA(CLOSE, 20)为20日均线）
    return pd.Series(S).rolling(N).mean().values


def EMA(S, N):  # 指数移动平均（如EMA(CLOSE, 12)为12日指数均线）
    return pd.Series(S).ewm(span=N, adjust=False).mean().values


def SMA(S, N, M=1):  # 中国式SMA（如KDJ中的平滑计算）
    return pd.Series(S).ewm(alpha=M / N, adjust=False).mean().values


def WMA(S, N):  # 加权移动平均（按时间加权，近期权重更高）
    return pd.Series(S).rolling(N).apply(lambda x: x[::-1].cumsum().sum() * 2 / N / (N + 1), raw=True).values


def DMA(S, A):  # 动态移动平均（A为平滑因子，支持序列输入）
    if isinstance(A, (int, float)):  return pd.Series(S).ewm(alpha=A, adjust=False).mean().values
    A = np.array(A);
    A[np.isnan(A)] = 1.0;
    Y = np.zeros(len(S));
    Y[0] = S[0]
    for i in range(1, len(S)): Y[i] = A[i] * S[i] + (1 - A[i]) * Y[i - 1]
    return Y


def AVEDEV(S, N):  # 平均绝对偏差（如CCI指标中的平均偏差计算）
    return pd.Series(S).rolling(N).apply(lambda x: (np.abs(x - x.mean())).mean()).values


def SLOPE(S, N):  # 线性回归斜率（如趋势线斜率）
    return pd.Series(S).rolling(N).apply(lambda x: np.polyfit(range(N), x, deg=1)[0], raw=True).values


def FORCAST(S, N):  # 线性回归预测值（如基于历史的未来值预测）
    return pd.Series(S).rolling(N).apply(lambda x: np.polyval(np.polyfit(range(N), x, deg=1), N - 1), raw=True).values


def LAST(S, A, B):  # A到B日前持续满足条件（如LAST(CLOSE>OPEN, 5, 1)表示近5日中前4日都收阳）
    return np.array(pd.Series(S).rolling(A + 1).apply(lambda x: np.all(x[::-1][B:]), raw=True), dtype=bool)


# ------------------ 1级：应用层函数（直接适配日线字段） --------------------------------
def COUNT(S, N):  # N日内满足条件的天数（如COUNT(CLOSE>OPEN, 5)为近5日阳线数）
    return SUM(S, N)


def EVERY(S, N):  # N日内全部满足条件（如EVERY(CLOSE>MA(CLOSE,20), 5)为近5日都在20日均线上）
    return IF(SUM(S, N) == N, True, False)


def EXIST(S, N):  # N日内存在满足条件（如EXIST(CLOSE>10%, 5)为近5日有涨停）
    return IF(SUM(S, N) > 0, True, False)


def FILTER(S, N):  # 条件成立后屏蔽后续N周期（如FILTER(CROSS(MA5, MA10), 3)为金叉后3日不重复提示）
    for i in range(len(S)):
        if S[i]: S[i + 1:i + 1 + N] = 0
    return S


def BARSLAST(S):  # 上一次条件成立到当前的周期数（如BARSLAST(CLOSE跌停)为上次跌停至今天数）
    M = np.concatenate(([0], np.where(S, 1, 0)))
    for i in range(1, len(M)): M[i] = 0 if M[i] else M[i - 1] + 1
    return M[1:]


def BARSLASTCOUNT(S):  # 连续满足条件的周期数（如BARSLASTCOUNT(CLOSE>OPEN)为连续阳线数）
    rt = np.zeros(len(S) + 1)
    for i in range(len(S)): rt[i + 1] = rt[i] + 1 if S[i] else rt[i + 1]
    return rt[1:]


def BARSSINCEN(S, N):  # N周期内首次满足条件到现在的周期数（如BARSSINCEN(CLOSE>MA20, 20)为20日内首次上穿均线至今天数）
    return pd.Series(S).rolling(N).apply(lambda x: N - 1 - np.argmax(x) if np.argmax(x) or x[0] else 0,
                                         raw=True).fillna(0).values.astype(int)


def CROSS(S1, S2):  # 向上金叉（如CROSS(MA(CLOSE,5), MA(CLOSE,10))为5日均线上穿10日线）
    return np.concatenate(([False], np.logical_not((S1 > S2)[:-1]) & (S1 > S2)[1:]))


def LONGCROSS(S1, S2, N):  # 持续N周期后交叉（如LONGCROSS(MA5, MA10, 3)为5日线在3日内始终低于10日线后上穿）
    return np.array(np.logical_and(LAST(S1 < S2, N, 1), (S1 > S2)), dtype=bool)


def VALUEWHEN(S, X):  # 条件成立时记录X值（如VALUEWHEN(CROSS(MA5, MA10), CLOSE)为金叉时的收盘价）
    return pd.Series(np.where(S, X, np.nan)).ffill().values


def BETWEEN(S, A, B):  # S在A和B之间（如BETWEEN(CLOSE, MA20*0.98, MA20*1.02)为收盘价在20均线附近）
    return ((A < S) & (S < B)) | ((A > S) & (S > B))


def TOPRANGE(S):  # 当前值为近多少周期内的最大值（如TOPRANGE(HIGH)为当前最高价是近几日最高价）
    rt = np.zeros(len(S))
    for i in range(1, len(S)): rt[i] = np.argmin(np.flipud(S[:i] < S[i]))
    return rt.astype('int')


def LOWRANGE(S):  # 当前值为近多少周期内的最小值（如LOWRANGE(LOW)为当前最低价是近几日最低价）
    rt = np.zeros(len(S))
    for i in range(1, len(S)): rt[i] = np.argmin(np.flipud(S[:i] > S[i]))
    return rt.astype('int')


# ------------------ 2级：技术指标函数（明确字段依赖） ------------------------------
def MACD(CLOSE, SHORT=12, LONG=26, M=9):
    # 输入：CLOSE（对应"收盘价"字段）
    DIF = EMA(CLOSE, SHORT) - EMA(CLOSE, LONG)
    DEA = EMA(DIF, M)
    MACD = (DIF - DEA) * 2
    return RD(DIF), RD(DEA), RD(MACD)


def KDJ(CLOSE, HIGH, LOW, N=9, M1=3, M2=3):
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    RSV = (CLOSE - LLV(LOW, N)) / (HHV(HIGH, N) - LLV(LOW, N)) * 100
    K = EMA(RSV, (M1 * 2 - 1))
    D = EMA(K, (M2 * 2 - 1))
    J = K * 3 - D * 2
    return K, D, J


def RSI(CLOSE, N=24):
    # 输入：CLOSE（收盘价）
    DIF = CLOSE - REF(CLOSE, 1)
    return RD(SMA(MAX(DIF, 0), N) / SMA(ABS(DIF), N) * 100)


def WR(CLOSE, HIGH, LOW, N=10, N1=6):
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    WR = (HHV(HIGH, N) - CLOSE) / (HHV(HIGH, N) - LLV(LOW, N)) * 100
    WR1 = (HHV(HIGH, N1) - CLOSE) / (HHV(HIGH, N1) - LLV(LOW, N1)) * 100
    return RD(WR), RD(WR1)


def BIAS(CLOSE, L1=6, L2=12, L3=24):
    # 输入：CLOSE（收盘价）
    BIAS1 = (CLOSE - MA(CLOSE, L1)) / MA(CLOSE, L1) * 100
    BIAS2 = (CLOSE - MA(CLOSE, L2)) / MA(CLOSE, L2) * 100
    BIAS3 = (CLOSE - MA(CLOSE, L3)) / MA(CLOSE, L3) * 100
    return RD(BIAS1), RD(BIAS2), RD(BIAS3)


def BOLL(CLOSE, N=20, P=2):
    # 输入：CLOSE（收盘价）
    MID = MA(CLOSE, N)
    UPPER = MID + STD(CLOSE, N) * P
    LOWER = MID - STD(CLOSE, N) * P
    return RD(UPPER), RD(MID), RD(LOWER)


def PSY(CLOSE, N=12, M=6):
    # 输入：CLOSE（收盘价）
    PSY = COUNT(CLOSE > REF(CLOSE, 1), N) / N * 100
    PSYMA = MA(PSY, M)
    return RD(PSY), RD(PSYMA)


def CCI(CLOSE, HIGH, LOW, N=14):
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    TP = (HIGH + LOW + CLOSE) / 3
    return (TP - MA(TP, N)) / (0.015 * AVEDEV(TP, N))


def ATR(CLOSE, HIGH, LOW, N=20):
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    TR = MAX(MAX((HIGH - LOW), ABS(REF(CLOSE, 1) - HIGH)), ABS(REF(CLOSE, 1) - LOW))
    return MA(TR, N)


def BBI(CLOSE, M1=3, M2=6, M3=12, M4=20):
    # 输入：CLOSE（收盘价）
    return (MA(CLOSE, M1) + MA(CLOSE, M2) + MA(CLOSE, M3) + MA(CLOSE, M4)) / 4


def DMI(CLOSE, HIGH, LOW, M1=14, M2=6):
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    TR = SUM(MAX(MAX(HIGH - LOW, ABS(HIGH - REF(CLOSE, 1))), ABS(LOW - REF(CLOSE, 1))), M1)
    HD = HIGH - REF(HIGH, 1)
    LD = REF(LOW, 1) - LOW
    DMP = SUM(IF((HD > 0) & (HD > LD), HD, 0), M1)
    DMM = SUM(IF((LD > 0) & (LD > HD), LD, 0), M1)
    PDI = DMP * 100 / TR
    MDI = DMM * 100 / TR
    ADX = MA(ABS(MDI - PDI) / (PDI + MDI) * 100, M2)
    ADXR = (ADX + REF(ADX, M2)) / 2
    return PDI, MDI, ADX, ADXR


def TAQ(HIGH, LOW, N):
    # 输入：HIGH（最高价）、LOW（最低价）
    UP = HHV(HIGH, N)
    DOWN = LLV(LOW, N)
    MID = (UP + DOWN) / 2
    return UP, MID, DOWN


def KTN(CLOSE, HIGH, LOW, N=20, M=10):
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    MID = EMA((HIGH + LOW + CLOSE) / 3, N)
    ATRN = ATR(CLOSE, HIGH, LOW, M)
    UPPER = MID + 2 * ATRN
    LOWER = MID - 2 * ATRN
    return UPPER, MID, LOWER


def TRIX(CLOSE, M1=12, M2=20):
    # 输入：CLOSE（收盘价）
    TR = EMA(EMA(EMA(CLOSE, M1), M1), M1)
    TRIX = (TR - REF(TR, 1)) / REF(TR, 1) * 100
    TRMA = MA(TRIX, M2)
    return TRIX, TRMA


# ------------------ 2级：技术指标函数（续，明确字段依赖） ------------------------------

def VR(CLOSE, VOL, M1=26):  # VR容量比率
    # 输入：CLOSE（对应"收盘价"字段）、VOL（对应"成交量(手)"字段）
    LC = REF(CLOSE, 1)  # 前一日收盘价
    return SUM(IF(CLOSE > LC, VOL, 0), M1) / SUM(IF(CLOSE <= LC, VOL, 0), M1) * 100


def CR(CLOSE, HIGH, LOW, N=20):  # CR价格动量指标
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    MID = REF(HIGH + LOW + CLOSE, 1) / 3  # 前一日（最高价+最低价+收盘价）/3
    return SUM(MAX(0, HIGH - MID), N) / SUM(MAX(0, MID - LOW), N) * 100


def EMV(HIGH, LOW, VOL, N=14, M=9):  # 简易波动指标
    # 输入：HIGH（最高价）、LOW（最低价）、VOL（成交量(手)）
    VOLUME = MA(VOL, N) / VOL  # N日平均成交量与当日成交量比值
    MID = 100 * (HIGH + LOW - REF(HIGH + LOW, 1)) / (HIGH + LOW)  # 价幅变动比例
    EMV = MA(MID * VOLUME * (HIGH - LOW) / MA(HIGH - LOW, N), N)  # 简易波动值
    MAEMV = MA(EMV, M)  # EMV的M日移动平均
    return EMV, MAEMV


def DPO(CLOSE, M1=20, M2=10, M3=6):  # 区间震荡线
    # 输入：CLOSE（收盘价）
    DPO = CLOSE - REF(MA(CLOSE, M1), M2)  # 当前价与M1均线M2日前值的差
    MADPO = MA(DPO, M3)  # DPO的M3日移动平均
    return DPO, MADPO


def BRAR(OPEN, CLOSE, HIGH, LOW, M1=26):  # BRAR-ARBR 情绪指标
    # 输入：OPEN（开盘价）、CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    AR = SUM(HIGH - OPEN, M1) / SUM(OPEN - LOW, M1) * 100  # AR：N日（最高价-开盘价）与（开盘价-最低价）的比率
    BR = SUM(MAX(0, HIGH - REF(CLOSE, 1)), M1) / SUM(MAX(0, REF(CLOSE, 1) - LOW),
                                                     M1) * 100  # BR：N日（最高价-前日收盘价）与（前日收盘价-最低价）的比率
    return AR, BR


def DFMA(CLOSE, N1=10, N2=50, M=10):  # 平行线差指标（通达信叫DMA）
    # 输入：CLOSE（收盘价）
    DIF = MA(CLOSE, N1) - MA(CLOSE, N2)  # N1均线与N2均线差值
    DIFMA = MA(DIF, M)  # 差值的M日移动平均
    return DIF, DIFMA


def MTM(CLOSE, N=12, M=6):  # 动量指标
    # 输入：CLOSE（收盘价）
    MTM = CLOSE - REF(CLOSE, N)  # 当前价与N日前收盘价的差
    MTMMA = MA(MTM, M)  # 动量值的M日移动平均
    return MTM, MTMMA


def MASS(HIGH, LOW, N1=9, N2=25, M=6):  # 梅斯线
    # 输入：HIGH（最高价）、LOW（最低价）
    HIGH_LOW = HIGH - LOW  # 当日价幅
    MA_HL = MA(HIGH_LOW, N1)  # N1日价幅移动平均
    MA_MA_HL = MA(MA_HL, N1)  # N1日价幅均线的移动平均
    MASS = SUM(MA_HL / MA_MA_HL, N2)  # N2日（价幅均线/价幅均线的均线）的累计和
    MA_MASS = MA(MASS, M)  # 梅斯线的M日移动平均
    return MASS, MA_MASS


def ROC(CLOSE, N=12, M=6):  # 变动率指标
    # 输入：CLOSE（收盘价）
    ROC = 100 * (CLOSE - REF(CLOSE, N)) / REF(CLOSE, N)  # （当前价-前N日价）/前N日价 *100
    MAROC = MA(ROC, M)  # ROC的M日移动平均
    return ROC, MAROC


def EXPMA(CLOSE, N1=12, N2=50):  # EMA指数平均数指标
    # 输入：CLOSE（收盘价）
    return EMA(CLOSE, N1), EMA(CLOSE, N2)  # 两个不同周期的指数移动平均


def OBV(CLOSE, VOL):  # 能量潮指标
    # 输入：CLOSE（收盘价）、VOL（成交量(手)）
    # 规则：收盘价上涨时累加成交量，下跌时扣除成交量，结果除以10000（单位转换）
    return SUM(IF(CLOSE > REF(CLOSE, 1), VOL, IF(CLOSE < REF(CLOSE, 1), -VOL, 0)), 0) / 10000


def MFI(CLOSE, HIGH, LOW, VOL, N=14):  # MFI指标（成交量的RSI）
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）、VOL（成交量(手)）
    TYP = (HIGH + LOW + CLOSE) / 3  # 典型价格
    # 正资金流量（TYP上涨时TYP*VOL的和）与负资金流量（TYP下跌时TYP*VOL的和）的比率
    V1 = SUM(IF(TYP > REF(TYP, 1), TYP * VOL, 0), N) / SUM(IF(TYP < REF(TYP, 1), TYP * VOL, 0), N)
    return 100 - (100 / (1 + V1))  # 转换为0-100的指标值


def ASI(OPEN, CLOSE, HIGH, LOW, M1=26, M2=10):  # 振动升降指标
    # 输入：OPEN（开盘价）、CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    LC = REF(CLOSE, 1)  # 前一日收盘价
    AA = ABS(HIGH - LC)  # 最高价与前日收盘价差的绝对值
    BB = ABS(LOW - LC)  # 最低价与前日收盘价差的绝对值
    CC = ABS(HIGH - REF(LOW, 1))  # 最高价与前日最低价差的绝对值
    DD = ABS(LC - REF(OPEN, 1))  # 前日收盘价与前日开盘价差的绝对值

    # 计算基准值R（根据AA、BB、CC的大小选择不同计算方式）
    R = IF((AA > BB) & (AA > CC), AA + BB / 2 + DD / 4,
           IF((BB > CC) & (BB > AA), BB + AA / 2 + DD / 4, CC + DD / 4))

    X = (CLOSE - LC) + (CLOSE - OPEN) / 2 + (LC - REF(OPEN, 1))  # 综合价格变动值
    SI = 16 * X / R * MAX(AA, BB)  # 单根K线的振动值
    ASI = SUM(SI, M1)  # M1日SI的累计和
    ASIT = MA(ASI, M2)  # ASI的M2日移动平均
    return ASI, ASIT


def XSII(CLOSE, HIGH, LOW, N=102, M=7):  # 薛斯通道II
    # 输入：CLOSE（收盘价）、HIGH（最高价）、LOW（最低价）
    AA = MA((2 * CLOSE + HIGH + LOW) / 4, 5)  # 5日（2收盘价+最高价+最低价）/4的移动平均
    TD1 = AA * N / 100  # 通道上轨1（N%比例）
    TD2 = AA * (200 - N) / 100  # 通道下轨1（(200-N)%比例）

    # 计算价格偏离度CC
    CC = ABS((2 * CLOSE + HIGH + LOW) / 4 - MA(CLOSE, 20)) / MA(CLOSE, 20)
    DD = DMA(CLOSE, CC)  # 动态移动平均（CC为平滑因子）
    TD3 = (1 + M / 100) * DD  # 通道上轨2（M%比例）
    TD4 = (1 - M / 100) * DD  # 通道下轨2（-M%比例）
    return TD1, TD2, TD3, TD4


# ------------------ 0级扩展：支持动态周期的核心函数 --------------------------------------------

def HHV(S, N):
    """
    计算N周期内的最高价（支持N为固定值或动态序列）
    输入：S（价格序列，如HIGH最高价字段）、N（周期数，整数或与S等长的序列）
    输出：等长于S的最高价序列
    示例：HHV(HIGH, 5)  # 最近5日最高价；HHV(CLOSE, N序列)  # 每个位置用对应N值计算高点
    """
    if isinstance(N, (int, float)):
        return pd.Series(S).rolling(N).max().values  # 固定周期：用pandas滚动窗口计算
    else:
        res = np.repeat(np.nan, len(S))  # 初始化结果为nan
        for i in range(len(S)):
            if (not np.isnan(N[i])) and N[i] <= i + 1:  # 周期数有效且不超过当前位置
                res[i] = S[i + 1 - int(N[i]):i + 1].max()  # 动态周期：截取对应长度计算高点
        return res


def LLV(S, N):
    """
    计算N周期内的最低价（支持N为固定值或动态序列）
    输入：S（价格序列，如LOW最低价字段）、N（周期数，整数或与S等长的序列）
    输出：等长于S的最低价序列
    示例：LLV(LOW, 5)  # 最近5日最低价；LLV(CLOSE, N序列)  # 每个位置用对应N值计算低点
    """
    if isinstance(N, (int, float)):
        return pd.Series(S).rolling(N).min().values  # 固定周期：用pandas滚动窗口计算
    else:
        res = np.repeat(np.nan, len(S))  # 初始化结果为nan
        for i in range(len(S)):
            if (not np.isnan(N[i])) and N[i] <= i + 1:  # 周期数有效且不超过当前位置
                res[i] = S[i + 1 - int(N[i]):i + 1].min()  # 动态周期：截取对应长度计算低点
        return res


# ------------------ 0级扩展：高级移动平均函数 --------------------------------------------

def DSMA(X, N):
    """
    偏差自适应移动平均线（Deviation Scaled Moving Average）
    输入：X（价格序列，如CLOSE收盘价字段）、N（基准周期数）
    输出：自适应平滑后的移动平均序列
    原理：通过价格波动的偏差调整平滑因子，波动大时更敏感，波动小时更平滑
    """
    a1 = math.exp(-1.414 * math.pi * 2 / N)  # 系数计算（基于周期N的指数衰减）
    b1 = 2 * a1 * math.cos(1.414 * math.pi * 2 / N)  # 余弦项系数
    c2 = b1
    c3 = -a1 * a1  # 平方项系数
    c1 = 1 - c2 - c3  # 剩余系数

    # 计算价格变化率（Zeros为X的二阶差分）
    Zeros = np.pad(X[2:] - X[:-2], (2, 0), 'constant')  # 填充前两个位置为0

    Filt = np.zeros(len(X))  # 初始化滤波值
    for i in range(len(X)):
        # 递归计算滤波值（考虑前两项的影响）
        Filt[i] = c1 * (Zeros[i] + Zeros[i - 1]) / 2 + c2 * Filt[i - 1] + c3 * Filt[i - 2]

    # 计算滤波值的N周期均方根（RMS）
    RMS = np.sqrt(SUM(np.square(Filt), N) / N)

    # 标准化滤波值并计算自适应平滑因子alpha1
    ScaledFilt = Filt / RMS  # 标准化（消除量纲影响）
    alpha1 = np.abs(ScaledFilt) * 5 / N  # 平滑因子（波动越大，alpha越大，响应越快）

    return DMA(X, alpha1)  # 用动态移动平均（DMA）生成最终均线


# ------------------ 1级扩展：累计周期计算函数 --------------------------------------------

def SUMBARSFAST(X, A):
    """
    计算X累加至A的周期数（类似通达信SumBars）
    输入：X（被累计的序列，如VOL成交量字段，需全为正数）、A（目标累计值，单值或与X等长的序列）
    输出：等长于X的周期数序列（每个位置表示从该位置向前累加至A所需的周期数）
    示例：SUMBARSFAST(VOL, 100000)  # 成交量累加至10万股的周期数；SUMBARSFAST(VOL, CAPITAL)  # 完全换手周期数
    """
    if any(X <= 0):  # 检查X是否全为正数（否则无法累加）
        raise ValueError('数组X的每个元素都必须大于0！')

    X = np.flipud(X)  # 倒转X（从后往前处理）
    length = len(X)

    if isinstance(A * 1.0, float):  # 若A是单值，扩展为与X等长的序列
        A = np.repeat(A, length)
    A = np.flipud(A)  # 倒转A（与X方向一致）

    sumbars = np.zeros(length)  # 初始化周期数结果
    Sigma = np.insert(np.cumsum(X), 0, 0.0)  # 累加前缀和（前面插入0便于索引）

    for i in range(length):
        # 查找累加和超过A[i]的位置
        k = np.searchsorted(Sigma[i + 1:], A[i] + Sigma[i])
        if k < length - i:  # 找到有效位置
            sumbars[length - i - 1] = k + 1  # 转换回原顺序的周期数
    return sumbars.astype(int)


# ------------------ 2级扩展：技术指标函数 --------------------------------------------

def SAR(HIGH, LOW, N=10, S=2, M=20):
    """
    抛物转向指标（Parabolic SAR）
    输入：HIGH（最高价字段）、LOW（最低价字段）、N（初始计算周期）、S（步长%）、M（步长极限%）
    输出：等长于HIGH的抛物转向序列（SAR值）
    说明：SAR是趋势跟踪指标，多空分界点，价格在SAR上方为多头，下方为空头
    """
    f_step = S / 100  # 步长因子（如S=2对应0.02）
    f_max = M / 100  # 步长极限（如M=20对应0.2）
    af = 0.0  # 加速因子（Acceleration Factor）
    is_long = HIGH[N - 1] > HIGH[N - 2]  # 初始趋势（多头/空头）
    b_first = True  # 是否为趋势起始点
    length = len(HIGH)

    # 计算初始极值（前N日高点/低点）
    s_hhv = REF(HHV(HIGH, N), 1)  # 前一日的N日最高价（延迟1日）
    s_llv = REF(LLV(LOW, N), 1)  # 前一日的N日最低价（延迟1日）
    sar_x = np.repeat(np.nan, length)  # 初始化SAR序列

    for i in range(N, length):
        if b_first:  # 趋势起始点
            af = f_step  # 重置加速因子为步长
            sar_x[i] = s_llv[i] if is_long else s_hhv[i]  # 初始SAR值（多头取前N日低点，空头取前N日高点）
            b_first = False
        else:  # 趋势延续中
            ep = s_hhv[i] if is_long else s_llv[i]  # 当前趋势的极值（多头为新高，空头为新低）
            # 若价格创新极值，增加加速因子（不超过极限）
            if (is_long and HIGH[i] > ep) or ((not is_long) and LOW[i] < ep):
                af = min(af + f_step, f_max)
            # 计算SAR值：前一日SAR + 加速因子*(极值 - 前一日SAR)
            sar_x[i] = sar_x[i - 1] + af * (ep - sar_x[i - 1])

        # 检查趋势反转（价格跌破/突破SAR）
        if (is_long and LOW[i] < sar_x[i]) or ((not is_long) and HIGH[i] > sar_x[i]):
            is_long = not is_long  # 反转趋势
            b_first = True  # 标记为新趋势起始点
    return sar_x


def TDX_SAR(High, Low, iAFStep=2, iAFLimit=20):
    """
    通达信版本抛物转向指标（与通达信SAR完全一致）
    输入：High（最高价字段）、Low（最低价字段）、iAFStep（步长%）、iAFLimit（步长极限%）
    输出：等长于High的抛物转向序列（SAR值）
    说明：与通用SAR算法差异在于极值修正和反转逻辑，更贴近通达信实际显示效果
    """
    af_step = iAFStep / 100  # 步长因子（如iAFStep=2对应0.02）
    af_limit = iAFLimit / 100  # 步长极限（如iAFLimit=20对应0.2）
    SarX = np.zeros(len(High))  # 初始化SAR序列

    # 第一个K线：默认多头，SAR初始为当日低点
    bull = True
    af = af_step  # 初始加速因子
    ep = High[0]  # 初始极值（多头为最高价）
    SarX[0] = Low[0]  # 初始SAR值

    # 从第二个K线开始计算
    for i in range(1, len(High)):
        # 1. 更新极值和加速因子（趋势延续时）
        if bull:  # 多头趋势
            if High[i] > ep:  # 创新高，更新极值并增加加速因子
                ep = High[i]
                af = min(af + af_step, af_limit)
        else:  # 空头趋势
            if Low[i] < ep:  # 创新低，更新极值并增加加速因子
                ep = Low[i]
                af = min(af + af_step, af_limit)

        # 2. 计算SAR值（基于前一日SAR和加速因子）
        SarX[i] = SarX[i - 1] + af * (ep - SarX[i - 1])

        # 3. 修正SAR值（避免穿透前两日价格）
        if bull:
            # 多头时SAR不低于前两日低点的最小值
            SarX[i] = max(SarX[i - 1], min(SarX[i], Low[i], Low[i - 1]))
        else:
            # 空头时SAR不高于前两日高点的最大值
            SarX[i] = min(SarX[i - 1], max(SarX[i], High[i], High[i - 1]))

        # 4. 检查趋势反转（价格突破SAR）
        if bull:  # 多头趋势中，价格跌破SAR则转空
            if Low[i] < SarX[i]:
                bull = False
                tmp_SarX = ep  # 前阶段的最高点（反转后的SAR初始值）
                ep = Low[i]  # 新趋势的极值（空头为当前低点）
                af = af_step  # 重置加速因子
                # 修正反转后的SAR值（避免跳空）
                if High[i - 1] == tmp_SarX:
                    SarX[i] = tmp_SarX  # 前一日是极值点，SAR保持极值
                else:
                    SarX[i] = tmp_SarX + af * (ep - tmp_SarX)
        else:  # 空头趋势中，价格突破SAR则转多
            if High[i] > SarX[i]:
                bull = True
                ep = High[i]  # 新趋势的极值（多头为当前高点）
                af = af_step  # 重置加速因子
                # 修正反转后的SAR值（取前两日低点的最小值）
                SarX[i] = min(Low[i], Low[i - 1])

    return SarX
