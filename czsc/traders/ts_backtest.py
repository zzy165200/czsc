# -*- coding: utf-8 -*-
"""
author: zengbin93
email: zeng_bin8888@163.com
create_dt: 2022/2/14 17:25
describe: 基于 Tushare 分钟数据的择时策略快速回测
"""

import os
import inspect
import traceback
import numpy as np
import pandas as pd
from tqdm import tqdm
from typing import Callable

from ..data.ts_cache import TsDataCache
from ..traders.utils import trader_fast_backtest, freq_cn2ts
from ..utils import WordWriter, x_round


def read_raw_results(raw_path, trade_dir="long"):
    """读入指定路径下的回测原始结果

    :param raw_path: 原始结果路径
    :param trade_dir: 交易方向
    :return:
    """
    assert trade_dir in ['long', 'short']

    ops, pairs, p = [], [], []
    for file in tqdm(os.listdir(raw_path)):
        if len(file) != 14:
            continue
        file = os.path.join(raw_path, file)
        ops.append(pd.read_excel(file, sheet_name=f'{trade_dir}_operates'))
        pairs.append(pd.read_excel(file, sheet_name=f'{trade_dir}_pairs'))
        p.append(pd.read_excel(file, sheet_name=f'{trade_dir}_performance'))

    df_pairs = pd.concat(pairs, ignore_index=True)
    df_ops = pd.concat(ops, ignore_index=True)
    df_p = pd.concat(p, ignore_index=True)
    return df_ops, df_pairs, df_p


class TraderPerformance:
    """择时交易效果评估"""
    def __init__(self, df_pairs: pd.DataFrame,):
        """

        :param df_pairs: 全部交易对
        """
        df_pairs['开仓年'] = df_pairs['开仓时间'].apply(lambda x: x.strftime("%Y年"))
        df_pairs['平仓年'] = df_pairs['平仓时间'].apply(lambda x: x.strftime("%Y年"))

        df_pairs['开仓月'] = df_pairs['开仓时间'].apply(lambda x: x.strftime("%Y年%m月"))
        df_pairs['平仓月'] = df_pairs['平仓时间'].apply(lambda x: x.strftime("%Y年%m月"))

        df_pairs['开仓周'] = df_pairs['开仓时间'].apply(
            lambda x: f"{x.year}年第{x.weekofyear}周" if x.weekofyear >= 10 else f"{x.year}年第0{x.weekofyear}周")
        df_pairs['平仓周'] = df_pairs['平仓时间'].apply(
            lambda x: f"{x.year}年第{x.weekofyear}周" if x.weekofyear >= 10 else f"{x.year}年第0{x.weekofyear}周")

        df_pairs['开仓日'] = df_pairs['开仓时间'].apply(lambda x: x.strftime("%Y-%m-%d"))
        df_pairs['平仓日'] = df_pairs['平仓时间'].apply(lambda x: x.strftime("%Y-%m-%d"))

        self.df_pairs = df_pairs
        # 指定哪些列可以用来进行聚合分析
        self.agg_columns = ['标的代码', '交易方向', '开仓年', '平仓年', '开仓月', '平仓月',
                            '开仓周', '平仓周', '开仓日', '平仓日']

    @staticmethod
    def get_pairs_statistics(df_pairs: pd.DataFrame):
        """统计一组交易的基本信息

        :param df_pairs:
        :return:
        """
        if len(df_pairs) == 0:
            info = {
                "交易标的数量": 0,
                "总体交易次数": 0,
                "平均持仓天数": 0,

                "平均单笔收益": 0,
                "最大单笔收益": 0,
                "最小单笔收益": 0,

                "交易胜率": 0,
                "累计盈亏比": 0,
                "交易得分": 0,
                "每自然日收益": 0,
            }
            return info

        win_pct = x_round(len(df_pairs[df_pairs['盈亏比例'] > 0]) / len(df_pairs), 4)
        df_gain = df_pairs[df_pairs['盈亏比例'] > 0]
        df_loss = df_pairs[df_pairs['盈亏比例'] <= 0]
        gain = df_gain['盈亏比例'].sum()
        loss = abs(df_loss['盈亏比例'].sum())

        # 限制累计盈亏比最大有效值
        gain_loss_rate = min(x_round(gain / (loss + 0.000001), 2), 5)

        info = {
            "交易标的数量": df_pairs['标的代码'].nunique(),
            "总体交易次数": len(df_pairs),
            "平均持仓天数": x_round(df_pairs['持仓天数'].mean(), 2),

            "平均单笔收益": x_round(df_pairs['盈亏比例'].mean() * 10000, 2),
            "最大单笔收益": x_round(df_pairs['盈亏比例'].max() * 10000, 2),
            "最小单笔收益": x_round(df_pairs['盈亏比例'].min() * 10000, 2),

            "交易胜率": win_pct,
            "累计盈亏比": gain_loss_rate,
            "交易得分": x_round(gain_loss_rate * win_pct, 4),
        }

        info['每自然日收益'] = x_round(info['平均单笔收益'] / info['平均持仓天数'], 2)
        return info

    def agg_statistics(self, col: str):
        """按列聚合进行交易对评价"""
        df_pairs = self.df_pairs.copy()
        assert col in self.agg_columns, f"{col} 不是支持聚合的列，参考：{self.agg_columns}"

        results = []
        for name, dfg in df_pairs.groupby(col):
            if dfg.empty:
                continue

            res = {col: name}
            res.update(self.get_pairs_statistics(dfg))
            results.append(res)
        df = pd.DataFrame(results)
        return df

    @property
    def basic_info(self):
        """写入基础信息"""
        df_pairs = self.df_pairs.copy()
        return self.get_pairs_statistics(df_pairs)

    def agg_to_excel(self, file_xlsx):
        """遍历聚合列，保存结果到 Excel 文件中"""
        f = pd.ExcelWriter(file_xlsx)
        for col in self.agg_columns:
            df_ = self.agg_statistics(col)
            df_.to_excel(f, sheet_name=f"{col}聚合", index=False)
        f.close()
        print(f"聚合分析结果文件：{file_xlsx}")


class TsStocksBacktest:
    """基于 Tushare 数据的择时回测系统（股票市场）"""

    def __init__(self,
                 dc: TsDataCache,
                 strategy: Callable,
                 init_n: int,
                 sdt: str,
                 edt: str,
                 ):
        """

        :param dc: Tushare 数据缓存对象
        :param strategy: 股票择时策略
        :param init_n: 初始化 Trader 需要的最少基础K线数量
        :param sdt: 开始回测时间
        :param edt: 结束回测时间
        """
        self.name = self.__class__.__name__
        self.strategy = strategy
        self.init_n = init_n
        self.data_path = dc.data_path
        self.res_path = os.path.join(self.data_path, strategy.__name__)
        os.makedirs(self.res_path, exist_ok=True)

        file_strategy = os.path.join(self.res_path, f'{strategy.__name__}_strategy.txt')
        with open(file_strategy, 'w', encoding='utf-8') as f:
            f.write(inspect.getsource(strategy))
        print(f"strategy saved into {file_strategy}")

        self.dc = dc
        self.sdt = sdt
        self.edt = edt

        stocks = self.dc.stock_basic()
        stocks_ = stocks[stocks['list_date'] < '2010-01-01'].ts_code.to_list()
        self.stocks_map = {
            "index": ['000905.SH', '000016.SH', '000300.SH'],
            "stock": stocks.ts_code.to_list(),
            "check": ['000001.SZ'],
            "train": stocks_[:200],
            "valid": stocks_[200:600],
        }

    def analyze_results(self, step, trade_dir="long"):
        res_path = self.res_path
        raw_path = os.path.join(res_path, f'raw_{step}')
        df_ops, df_pairs, df_p = read_raw_results(raw_path, trade_dir)

        df_ops.to_csv(os.path.join(res_path, f"{self.strategy.__name__}_{step}_{trade_dir}_operates.csv"), index=False)
        df_pairs.to_excel(os.path.join(res_path, f"{self.strategy.__name__}_{step}_{trade_dir}_pairs.xlsx"), index=False)

        f = pd.ExcelWriter(os.path.join(res_path, f"{self.strategy.__name__}_{step}_{trade_dir}_performance.xlsx"))
        df_p.to_excel(f, sheet_name="评估", index=False)
        tp = TraderPerformance(df_pairs)
        for col in tp.agg_columns:
            df_ = tp.agg_statistics(col)
            df_.to_excel(f, sheet_name=f"{col}聚合", index=False)
        f.close()

    def batch_backtest(self, step):
        """批量回测

        :param step: 择时策略研究阶段
            check   在给定的股票上观察策略交易的准确性，输出交易快照
            index   在股票指数上评估策略表现
            train   在训练集上评估策略表现
            valid   在验证集上评估策略表现
            stock   用全市场所有股票评估策略表现
        :return:
        """
        assert step in self.stocks_map.keys(), f"step 参数输入错误，可选值：{list(self.stocks_map.keys())}"

        init_n = self.init_n
        if step == 'check':
            save_html = True
        else:
            save_html = False
        ts_codes = self.stocks_map[step]
        dc = self.dc
        res_path = self.res_path
        sdt = self.sdt
        edt = self.edt
        strategy = self.strategy
        raw_path = os.path.join(res_path, f"raw_{step}")
        os.makedirs(raw_path, exist_ok=True)

        if step == 'index':
            asset = "I"
        else:
            asset = 'E'

        tactic = strategy()
        base_freq = tactic['base_freq']
        signals_n = tactic.get('signals_n', 0)
        assert signals_n >= 0

        with open(os.path.join(res_path, f'{strategy.__name__}_strategy.txt'), 'w', encoding='utf-8') as f:
            f.write(inspect.getsource(strategy))

        for ts_code in ts_codes:
            if save_html:
                html_path = os.path.join(res_path, f"raw_{step}/{ts_code}")
                os.makedirs(html_path, exist_ok=True)
            else:
                html_path = None

            try:
                file_res = os.path.join(res_path, f"raw_{step}/{ts_code}.xlsx")
                if os.path.exists(file_res):
                    print(f"exits: {file_res}")
                    continue

                if "分钟" in base_freq:
                    bars = dc.pro_bar_minutes(ts_code, sdt, edt, freq=freq_cn2ts[base_freq],
                                              asset=asset, adj='hfq', raw_bar=True)
                else:
                    bars = dc.pro_bar(ts_code, sdt, edt, freq=freq_cn2ts[base_freq],
                                      asset=asset, adj='hfq', raw_bar=True)
                res = trader_fast_backtest(bars, init_n, strategy, html_path, signals_n=signals_n)

                f = pd.ExcelWriter(file_res)
                if res.get('long_performance', None):
                    print(f"{strategy.__name__} long_performance: {res['long_performance']}")
                    pd.DataFrame(res['long_operates']).to_excel(f, sheet_name="long_operates", index=False)
                    pd.DataFrame(res['long_pairs']).to_excel(f, sheet_name="long_pairs", index=False)
                    pd.DataFrame([res['long_performance']]).to_excel(f, sheet_name="long_performance", index=False)

                if res.get('short_performance', None):
                    print(f"{strategy.__name__} short_performance: {res['short_performance']}")
                    pd.DataFrame(res['short_operates']).to_excel(f, sheet_name="short_operates", index=False)
                    pd.DataFrame(res['short_pairs']).to_excel(f, sheet_name="short_pairs", index=False)
                    pd.DataFrame([res['short_performance']]).to_excel(f, sheet_name="short_performance", index=False)

                f.close()
            except:
                traceback.print_exc()

        if tactic.get('long_events', None):
            self.analyze_results(step, 'long')
        if tactic.get('short_events', None):
            self.analyze_results(step, 'short')
        print(f"results saved into {self.res_path}")


