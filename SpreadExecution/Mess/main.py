import datetime
from SpreadExecution.Mess.backtest import Backtest
from concurrent.futures import ProcessPoolExecutor


def one_run(param: int):
    my_backtest = Backtest(random_start=True, parameter=param)
    my_backtest.run()
    if (my_backtest.spot_position == my_backtest.target_size) and \
            (my_backtest.future_position == -my_backtest.target_size):
        execution_start = min(my_backtest.backtest_spot_trades['timestamp'].iloc[0],
                              my_backtest.backtest_future_trades['timestamp'].iloc[0])
        execution_end = max(my_backtest.backtest_spot_trades['timestamp'].iloc[-1],
                            my_backtest.backtest_future_trades['timestamp'].iloc[-1])
        print('\n')
        print('minimum_basis_spread: ' + str(param) + ' bps.')
        print('Execution from ' + datetime.datetime.fromtimestamp(execution_start // 1000000000).strftime(
            '%Y-%m-%d %H:%M:%S') + ' to ' + datetime.datetime.fromtimestamp(execution_end // 1000000000).strftime(
            '%Y-%m-%d %H:%M:%S') + ' in ' + str(int((execution_end - execution_start)//60000000000)) + ' minutes.')
        average_future_price = my_backtest.backtest_future_trades.future_trade_price.mean()
        average_spot_price = my_backtest.backtest_spot_trades.spot_trade_price.mean()
        executed_premium = 2e4 * (average_future_price - average_spot_price) / (average_future_price +
                                                                                average_spot_price)
        print('Executed premium: ' + str(executed_premium) + ' bp')
        print('\n')


def main():
    for i in range(20):
        print('\n*****************************\n')
        with ProcessPoolExecutor(8) as pool:
            for j in range(8):
                pool.submit(one_run, i)


if __name__ == '__main__':
    start_time = datetime.datetime.now()
    main()
    end_time = datetime.datetime.now()
    print('--- ran in ' + str(end_time - start_time))
