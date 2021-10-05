import datetime
from concurrent.futures import ProcessPoolExecutor
import numpy as np
import backtest

start_time = datetime.datetime.now()


def f(gamma: float) -> None:
    print('running gamma = ' + str("{:,.2f}".format(gamma)))
    date = datetime.date(2021, 9, 1)
    my_backtest = backtest.Backtest(symbol='BTCUSDT', date=date, k=0.02, gamma=gamma, horizon=60)
    results = my_backtest.run()
    print(results)
    results.to_csv('C:/Users/Tibor/Sandbox/results_' + str("{:,.2f}".format(gamma)) + '.csv')


def main():
    gammas = np.concatenate((np.arange(0.01, 0.1, 0.01), np.arange(0.1, 1, 0.1)))
    with ProcessPoolExecutor(10) as pool:
        for gamma in gammas:
            pool.submit(f, gamma)

    end_time = datetime.datetime.now()
    print('--- ran in ' + str(end_time - start_time))


if __name__ == '__main__':
    main()

# start_time = datetime.datetime.now()
#
# for d in range(22):
#     date = datetime.date(2021, 9, 1) + datetime.timedelta(days=d)
#     my_market_data = SimpleMarketMaking.Clean.market_data.MarketData('BTCUSDT')
#     my_market_data.load_trade_data_from_parquet(date)
#     my_market_data.load_top_of_book_data_from_parquet(date)
#     my_market_data.generate_formatted_trades_data()
#     my_market_data.generate_formatted_top_of_book_data()
#     date_string = date.strftime('%Y%m%d')
#     my_market_data.trades_formatted.to_csv('C:/Users/Tibor/Data/formatted/trades/' + date_string +
#                                            '_Binance_BTCUSDT_trades.csv')
#     my_market_data.top_of_book_formatted.to_csv('C:/Users/Tibor/Data/formatted/tob/' + date_string +
#                                                 '_Binance_BTCUSDT_tob.csv')
#
# end_time = datetime.datetime.now()
# print('--- ran in ' + str(end_time - start_time))

# look_backs = range(1, 6)
# horizons = range(1, 6)
#
# n = len(data.index)
# returns = pd.DataFrame(index=data.index, columns=['horizon_' + str(horizon) for horizon in horizons])
# for i in data.index:
#     for horizon in horizons:
#         if i + horizon < n:
#             returns.iloc[i, horizon - 1] = (data.loc[i + horizon, 'vwap'] / data.loc[i, 'vwap']) - 1
#
# volume_imbalance = pd.DataFrame(index=data.index, columns=['look_back_' + str(look_back) for look_back in look_backs])
# cumulative_total_volume = 0
# cumulative_given_volume = 0
# cumulative_total_volumes = []
# cumulative_given_volumes = []
# for i in data.index:
#     cumulative_total_volume = cumulative_total_volume + data.loc[i, 'volume']
#     cumulative_given_volume = cumulative_given_volume + data.loc[i, 'volume_given']
#     cumulative_total_volumes.append(cumulative_total_volume)
#     cumulative_given_volumes.append(cumulative_given_volume)
#     for look_back in look_backs:
#         if i >= look_back:
#             total_volume = cumulative_total_volumes[i] - cumulative_total_volumes[i - look_back]
#             given_volume = cumulative_given_volumes[i] - cumulative_given_volumes[i - look_back]
#             imbalance = (total_volume - (2 * given_volume)) / total_volume
#             volume_imbalance.iloc[i, look_back - 1] = imbalance
#         elif i == look_back - 1:
#             total_volume = cumulative_total_volumes[i]
#             given_volume = cumulative_given_volumes[i]
#             imbalance = (total_volume - (2 * given_volume)) / total_volume
#             volume_imbalance.iloc[i, look_back - 1] = imbalance


# print('secondly bars')
# secondly_bars = my_market_data.get_time_bars(1000)
# print(secondly_bars)
# secondly_bars.to_csv('C:/Users/Tibor/Data/formatted/20210901_Binance_BTCUSDT_secondly_bars.csv')
#
# print('tick bars')
# tick_bars = my_market_data.get_tick_bars(18)
# print(tick_bars)
# tick_bars.to_csv('C:/Users/Tibor/Sandbox/tick_bars.csv')
#
# print('volume bars')
# volume_bars = my_market_data.get_volume_bars(4050)
# print(volume_bars)
# volume_bars.to_csv('C:/Users/Tibor/Sandbox/volume_bars.csv')
#
# print('dollar bars')
# dollar_bars = my_market_data.get_dollar_bars(2000000)
# print(dollar_bars)
# dollar_bars.to_csv('C:/Users/Tibor/Sandbox/dollar_bars.csv')
#
# print('tick imbalance bars')
# tick_imbalance_bars = my_market_data.get_tick_imbalance_bars()
# print(tick_imbalance_bars)
# tick_imbalance_bars.to_csv('C:/Users/Tibor/Sandbox/tick_imbalance_bars.csv')
#
# print('trade side imbalance bars')
# trade_side_imbalance_bars = my_market_data.get_trade_side_imbalance_bars()
# print(trade_side_imbalance_bars)
# trade_side_imbalance_bars.to_csv('C:/Users/Tibor/Sandbox/trade_side_imbalance_bars.csv')
#
# print('volume tick imbalance bars')
# volume_tick_imbalance_bars = my_market_data.get_volume_tick_imbalance_bars()
# print(volume_tick_imbalance_bars)
# volume_tick_imbalance_bars.to_csv('C:/Users/Tibor/Sandbox/volume_tick_imbalance_bars.csv')
#
# print('trade side imbalance bars')
# volume_trade_side_imbalance_bars = my_market_data.get_volume_trade_side_imbalance_bars()
# print(volume_trade_side_imbalance_bars)
# volume_trade_side_imbalance_bars.to_csv('C:/Users/Tibor/Sandbox/volume_trade_side_imbalance_bars.csv')
#
# print('dollar tick imbalance bars')
# dollar_tick_imbalance_bars = my_market_data.get_dollar_tick_imbalance_bars()
# print(dollar_tick_imbalance_bars)
# dollar_tick_imbalance_bars.to_csv('C:/Users/Tibor/Sandbox/dollar_tick_imbalance_bars.csv')
#
# print('dollar trade side imbalance bars')
# dollar_trade_side_imbalance_bars = my_market_data.get_dollar_trade_side_imbalance_bars()
# print(dollar_trade_side_imbalance_bars)
# dollar_trade_side_imbalance_bars.to_csv('C:/Users/Tibor/Sandbox/dollar_trade_side_imbalance_bars.csv')
#
# print('tick runs bars')
# tick_runs_bars = my_market_data.get_tick_runs_bars()
# print(tick_runs_bars)
# tick_runs_bars.to_csv('C:/Users/Tibor/Sandbox/tick_runs_bars.csv')
#
# print('trade side runs bars')
# trade_side_runs_bars = my_market_data.get_trade_side_runs_bars()
# print(trade_side_runs_bars)
# trade_side_runs_bars.to_csv('C:/Users/Tibor/Sandbox/trade_side_runs_bars.csv')
#
# print('volume tick runs bars')
# volume_tick_runs_bars = my_market_data.get_volume_tick_runs_bars()
# print(volume_tick_runs_bars)
# volume_tick_runs_bars.to_csv('C:/Users/Tibor/Sandbox/volume_tick_runs_bars.csv')
#
# print('volume trade side runs bars')
# volume_trade_side_runs_bars = my_market_data.get_volume_trade_side_runs_bars()
# print(volume_trade_side_runs_bars)
# volume_trade_side_runs_bars.to_csv('C:/Users/Tibor/Sandbox/volume_trade_side_runs_bars.csv')
#
# print('dollar tick runs bars')
# dollar_tick_runs_bars = my_market_data.get_dollar_tick_runs_bars()
# print(dollar_tick_runs_bars)
# dollar_tick_runs_bars.to_csv('C:/Users/Tibor/Sandbox/dollar_tick_runs_bars.csv')
#
# print('dollar trade side runs bars')
# dollar_trade_side_runs_bars = my_market_data.get_dollar_trade_side_runs_bars()
# print(dollar_trade_side_runs_bars)
# dollar_trade_side_runs_bars.to_csv('C:/Users/Tibor/Sandbox/dollar_trade_side_runs_bars.csv')
#