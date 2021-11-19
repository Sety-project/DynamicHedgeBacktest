import os

import numpy as np
import pandas as pd
from ftx_snap_basis import *
from ftx_portfolio import *
from ftx_ftx import *
import seaborn as sns
import ccxt
#from sklearn import *

def refresh_universe(exchange_name,sceening_mode):
    filename = sceening_mode+'.xlsx'
    if os.path.isfile(filename): return pd.read_excel(filename)

    exchange=open_exchange(exchange_name)
    futures = pd.DataFrame(fetch_futures(exchange, includeExpired=False)).set_index('name')
    markets=exchange.fetch_markets()

    universe_start = datetime(2021, 6, 1)
    universe_end = datetime(2021, 11, 1)
    borrow_decile = 0.1
    #type_allowed='perpetual'
    if sceening_mode == 'wide':
        future_volume_threshold = 2e5
        spot_volume_threshold = 2e5
        borrow_volume_threshold = 2e5
    elif sceening_mode == 'tight':
        future_volume_threshold = 5e6
        spot_volume_threshold = 5e6
        borrow_volume_threshold = 5e6

   # qualitative screening
    futures = futures[
        (futures['expired'] == False) & (futures['enabled'] == True) & (futures['type'] != "move")
        & (futures.apply(lambda f: float(find_spot_ticker(markets, f, 'ask')), axis=1) > 0.0)
        & (futures['tokenizedEquity'] != True)
        & (futures['spotMargin'] == True)]

    # volume screening
    hy_history = build_history(futures, exchange,
                               timeframe='1h', end=universe_end, start=universe_start,
                               dirname='archived data/universe')
    universe_filter_window=hy_history[universe_start:universe_end].index
    futures['borrow_volume_decile'] = futures.apply(lambda f:
                            (hy_history.loc[universe_filter_window,f['underlying']+'/rate/size']*hy_history.loc[universe_filter_window,f.name+'/mark/o']).quantile(q=borrow_decile),axis=1)
    futures['spot_volume_avg'] = futures.apply(lambda f:
                            (hy_history.loc[universe_filter_window,f['underlying'] + '/price/volume'] ).mean(),axis=1)
    futures['future_volume_avg'] = futures.apply(lambda f:
                            (hy_history.loc[universe_filter_window,f.name + '/price/volume']).mean(),axis=1)

    futures = futures[
          (futures['borrow_volume_decile'] > borrow_volume_threshold)
        & (futures['spot_volume_avg'] > spot_volume_threshold)
        & (futures['future_volume_avg'] > future_volume_threshold)]

    futures.to_excel(filename)

    return futures['symbol'].values
refresh_universe('ftx','wide')
refresh_universe('ftx','tight')

def perp_vs_cash_live(equity,
                signal_horizon,
                holding_period,
                concentration_limit,
                exclusion_list,
                run_dir=''):
    if os.path.isdir(run_dir):
        for file in os.listdir(run_dir): os.remove(run_dir+'/'+file)
    else: os.mkdir(run_dir)

    exchange = open_exchange('ftx')
    markets = exchange.fetch_markets()
    futures = pd.DataFrame(fetch_futures(exchange, includeExpired=False)).set_index('name')

    point_in_time = (datetime.now()-timedelta(hours=0)).replace(minute=0,second=0,microsecond=0)

    # filtering params
    universe=refresh_universe('ftx', 'wide')
    type_allowed = 'perpetual'
    max_nb_coins = 99
    carry_floor = 0.4

    # fee estimation params
    slippage_override = 0  # TODO: 2e-4  #### this is given by mktmaker
    slippage_scaler = 1
    slippage_orderbook_depth = 10000

    for (holding_period,signal_horizon,concentration_limit) in [(hp,sh,c) for hp in holding_period for sh in signal_horizon for c in concentration_limit]:

        ## ----------- enrich, get history, filter
        enriched = enricher(exchange, futures.drop(index=exclusion_list), holding_period, equity=equity,
                            slippage_override=slippage_override, slippage_orderbook_depth=slippage_orderbook_depth,
                            slippage_scaler=slippage_scaler,
                            params={'override_slippage': True, 'type_allowed': type_allowed, 'fee_mode': 'retail'})

        #### get history ( this is sloooow)
        hy_history = build_history(enriched, exchange,
                                   timeframe='1h', end=point_in_time, start=point_in_time-signal_horizon-holding_period,
                                   dirname='live_parquets')

        # ------- build derived data history
        (intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow) = build_derived_history(
            exchange, enriched, hy_history,
            holding_period,  # to convert slippage into rate
            signal_horizon)  # historical window for expectations)
        updated, marginFunc = update(enriched, point_in_time, hy_history, equity,
                                     intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow)
        # final filter, needs some history and good avg volumes
        pre_filtered = updated[
            (~np.isnan(updated['E_intCarry']))
            & (updated['type']==type_allowed)
            & (updated['symbol'].isin(universe['symbol']))]
        pre_filtered = pre_filtered.sort_values(by='E_intCarry', ascending=False).head(max_nb_coins)  # ,key=abs

        # run a trajectory
        optimized = pre_filtered
        updated, marginFunc = update(optimized, point_in_time, hy_history, equity,
                                     intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow)
        previous_weights = optimized['E_intCarry'] \
                           / (optimized['E_intCarry'].sum() if np.abs(optimized['E_intCarry'].sum()) > 0.1 else 0.1)

        optimized=cash_carry_optimizer(exchange,updated,marginFunc,
                                    previous_weights=previous_weights,
                                    holding_period = holding_period,
                                    signal_horizon=signal_horizon,
                                    concentration_limit=concentration_limit,
                                    equity=equity,
                                    optional_params= ['verbose']
                                  )

    optimized.to_excel(run_dir+'/optimal_live.xlsx')
    return optimized


def perp_vs_cash_backtest(
                equity,
                signal_horizon,
                holding_period,
                slippage_override,
                concentration_limit,
                filename='',
                optional_params=[]):
    exchange=open_exchange('ftx')
    markets = exchange.fetch_markets()
    futures = pd.DataFrame(fetch_futures(exchange,includeExpired=False)).set_index('name')

    # filtering params
    universe = refresh_universe('ftx', 'wide')
    type_allowed='perpetual'
    max_nb_coins = 99
    carry_floor = 0.4

    # fee estimation params
    slippage_scaler=1
    slippage_orderbook_depth=1000

    # backtest params
    backtest_start = datetime(2021, 7, 1)
    backtest_end = datetime(2021, 10, 1)

    ## ----------- enrich, get history, filter
    enriched=enricher(exchange, futures, holding_period,equity=equity,
                    slippage_override=slippage_override, slippage_orderbook_depth=slippage_orderbook_depth,
                    slippage_scaler=slippage_scaler,
                    params={'override_slippage': True,'type_allowed':type_allowed,'fee_mode':'retail'})

    #### get history ( this is sloooow)
    try:
        hy_history = from_parquet("DONOTDELETE_temporary_parquets/history.parquet")
        existing_futures = [name.split('/')[0] for name in hy_history.columns]
        new_futures = enriched[enriched['symbol'].isin(existing_futures)==False]
        if new_futures.empty==False:
            hy_history=pd.concat([hy_history,
                    build_history(new_futures,exchange,timeframe='1h',end=backtest_end,start=backtest_start-signal_horizon-holding_period)],
                    join='outer',axis=1)
            to_parquet(hy_history, "DONOTDELETE_temporary_parquets/history.parquet")
    except FileNotFoundError:
        hy_history = build_history(enriched,exchange,timeframe='1h',end=backtest_end,start=backtest_start-signal_horizon-holding_period)
        to_parquet(hy_history,"DONOTDELETE_temporary_parquets/history.parquet")

    # ------- build derived data history
    (intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow)=build_derived_history(
        exchange, enriched, hy_history,
        holding_period,  # to convert slippage into rate
        signal_horizon,filename)  # historical window for expectations)
    updated, marginFunc = update(enriched, backtest_end, hy_history, equity,
                                 intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow)
    # final filter, needs some history and good avg volumes
    pre_filtered=updated[
              (~np.isnan(updated['E_intCarry']))
            & (updated['type'] == type_allowed)
            & (updated['symbol'].isin(universe['symbol']))]
    pre_filtered = pre_filtered.sort_values(by='E_intCarry',ascending=False).head(max_nb_coins)#,key=abs

    # run a trajectory
    optimized=pre_filtered
    point_in_time=backtest_start+signal_horizon+holding_period # integrals not defined before that
    updated, marginFunc = update(optimized, backtest_start + signal_horizon + holding_period, hy_history, equity,
                                 intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow)
    previous_weights = equity * optimized['E_intCarry'] \
                       / (optimized['E_intCarry'].sum() if np.abs(optimized['E_intCarry'].sum()) > 0.1 else 0.1)
    previous_time=point_in_time
    trajectory=pd.DataFrame()

    while point_in_time<backtest_end:
        updated,excess_margin=update(pre_filtered,point_in_time,hy_history,equity,
                       intLongCarry, intShortCarry, intUSDborrow, E_long, E_short, E_intUSDborrow)
        optimized=cash_carry_optimizer(exchange,updated,excess_margin,
                                previous_weights=previous_weights,
                                holding_period = holding_period,
                                signal_horizon=signal_horizon,
                                concentration_limit=concentration_limit,
                                equity=equity,
                                optional_params= optional_params
                              )
        # need to assign RealizedCarry to previous_time
        if not trajectory.empty: trajectory.loc[trajectory['time']==previous_time,'RealizedCarry']=optimized['RealizedCarry'].values
        optimized['time'] = point_in_time

        # increment
        trajectory=trajectory.append(optimized.reset_index().rename({'name':'symbol'}),ignore_index=True)
        previous_weights = optimized['optimalWeight'].drop(index=['USD', 'total'])
        previous_time=point_in_time
        point_in_time += holding_period

    # remove last line because RealizedCarry is wrong there
    trajectory=trajectory.drop(trajectory[trajectory['time']==previous_time].index)

    #trajectory.xs('ask',level='field',axis=1)

#    with pd.ExcelWriter('summary_'+run_name+'.xlsx', engine='xlsxwriter') as writer:
#        trajectory.reorder_levels(['field','time'],axis='columns').to_excel(writer,'summary.xlsx')

    return trajectory

def timedeltatostring(dt):
    return str(dt.days)+'d'+str(int(dt.seconds/3600))+'h'
def run_ladder( concentration_limit_list,
                holding_period_list,
                signal_horizon_list,
                slippage_override,
                run_dir):
    if os.path.isdir(run_dir):
        for file in os.listdir(run_dir): os.remove(run_dir+'/'+file)
    else: os.mkdir(run_dir)

    ladder = pd.DataFrame()
    for c in concentration_limit_list:
        for h in holding_period_list:
            for s in signal_horizon_list:
                if s < h: continue
                for txcost in slippage_override:
                    run_name = 'concentration_limit_'+ str(c)\
                               +'_holding_period_' + timedeltatostring(h) \
                               + '_signal_horizon_' + timedeltatostring(s) \
                               + '_slippage_override_' + str(txcost)
                    trajectory=perp_vs_cash_backtest(equity=EQUITY,
                                      signal_horizon=s,
                                      holding_period=h,
                                      slippage_override=txcost,
                                      concentration_limit=c,
                                      filename='')#non verbose
                    #accrual[(c, h, s,)] = trajectory  # [(t,f)]
                    #for t in trajectory.columns.get_level_values('time').unique():
                    #    for f in trajectory.columns.get_level_values('field').unique():
                    #        accrual[(c,h,s,t,f)]=trajectory[(t,f)]
                    trajectory['slippage_override']=txcost
                    trajectory['concentration_limit'] = c
                    trajectory['signal_horizon'] = s
                    trajectory['holding_period'] = h
                    trajectory.to_excel(run_dir + '/' + run_name + '.xlsx')
                    ladder=ladder.append(trajectory,ignore_index=True)

    ladder.to_pickle(run_dir + '/ladder.pickle')

def run_benchmark_ladder(
                concentration_limit_list,
                slippage_override_list,
                run_dir):
    ladder = pd.DataFrame()
    #### first, pick best basket every hour, ignoring tx costs
    for c in concentration_limit_list:
            for txcost in slippage_override_list:
                trajectory = perp_vs_cash_backtest(equity=EQUITY,
                                                   signal_horizon=timedelta(hours=1),
                                                   holding_period=timedelta(hours=1),
                                                   slippage_override=txcost,
                                                   concentration_limit=c,
                                                   filename='cost_blind',
                                                   optional_params=['cost_blind'])
                trajectory['slippage_override'] = txcost
                trajectory['concentration_limit'] = c
                ladder = ladder.append(trajectory,ignore_index=True)

    ladder.to_pickle(run_dir + '/ladder.pickle')

if False:
    perp_vs_cash_live(equity=EQUITY,
                signal_horizon = [timedelta(days=7)],
                holding_period = [timedelta(days=2)],
                concentration_limit = [0.5],
                exclusion_list=[],
                run_dir='DONOTDELETE_live_parquets')
if True:
    run_benchmark_ladder(
                concentration_limit_list=[9, 1, .5],
                slippage_override_list=[2e-4],
                run_dir='DONOTDELETE_cost_blind')
if True:
    run_ladder( concentration_limit_list=[9, 1, .5],
                holding_period_list = [timedelta(hours=h) for h in [6,12]] + [timedelta(days=d) for d in [1, 2, 3, 4,5]],
                signal_horizon_list = [timedelta(hours=h) for h in [12]] + [timedelta(days=d) for d in [1, 2,3,4,5,7,10,30]],
                slippage_override = [2e-4],
                run_dir='DONOTDELETE_runs')