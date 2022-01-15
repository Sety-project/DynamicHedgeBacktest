# -*- coding: utf-8 -*-

# PLEASE DO NOT EDIT THIS FILE, IT IS GENERATED AND WILL BE OVERWRITTEN:
# https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md#how-to-contribute-code
import asyncio

import numpy as np
from ccxt.base.exchange import Exchange
from ftx_utilities import getUnderlyingType,find_spot_ticker
import pandas as pd
import dateutil
from datetime import *
import dateutil

# disregard pagination :(
async def vwap(exchange,symbol,start_time,end_time,freq):
    trade_list = pd.DataFrame(await exchange.publicGetMarketsMarketNameTrades(
        {'market_name': symbol, 'start_time': start_time/1000, 'end_time': end_time/1000}
    )['result'], dtype=float)
    trade_list['time']=trade_list['time'].apply(dateutil.parser.isoparse)
    trade_list['amt']=trade_list.apply(lambda x: x['size']*(1 if x['side'] else -1),axis=1)
    trade_list['amtUSD'] = trade_list['amt']*trade_list['price']

    vwap=trade_list.set_index('time')[['amt','amtUSD']].resample(freq).sum()
    vwap['vwap']=vwap['amtUSD']/vwap['amt']
    await asyncio.sleep(1)
    return vwap.drop(columns='amtUSD').ffill()

def underlying_vol(exchange,symbol,start_time,end_time):
    trade_list = pd.DataFrame(exchange.publicGetMarketsMarketNameTrades(
        {'market_name': symbol, 'start_time': start_time/1000, 'end_time': end_time/1000}
    )['result'], dtype=float)
    trade_list['timestamp']=trade_list['time'].apply(dateutil.parser.isoparse).apply(datetime.timestamp)
    trade_list['amt']=trade_list.apply(lambda x: x['size']*(1 if x['side'] else -1),axis=1)
    trade_list['amtUSD'] = trade_list['amt']*trade_list['price']

    # in case duplicated index
    trade_list=trade_list.set_index('timestamp')[['amt','amtUSD']].groupby(level=0).sum().reset_index()
    trade_list['price'] = trade_list['amtUSD']/trade_list['amt']
    vol = np.sqrt((trade_list['price'].diff()*trade_list['price'].diff()/trade_list['timestamp'].diff()).median())

    return vol

def mkt_at_size(exchange, symbol, side, target_depth=10000.):
    #side='bids' or 'asks'
    # returns average px of a mkt order of size target_depth (in USD)
    order_book = exchange.fetch_order_book(symbol)
    mktdepth = pd.DataFrame(order_book[side])
    other_side = 'bids' if side=='asks' else 'asks'
    mid = 0.5 * (order_book[side][0][0] + order_book[other_side][0][0])

    if target_depth==0:
        return (order_book[side][0][0],mid)

    mktdepth['px']=(mktdepth[0]*mktdepth[1]).cumsum()/mktdepth[1].cumsum()
    mktdepth['size']=(mktdepth[0]*mktdepth[1]).cumsum()

    interpolator=mktdepth.set_index('size')['px']
    interpolator[float(target_depth)]=np.NaN
    interpolator.interpolate(method='index',inplace=True)

    return {'mid':mid,'slippage':interpolator[target_depth]/mid-1.0}

def fetch_nearest_trade(exchange, symbol, time, target_depth=1000):

    ### first, find an interval with trades.
    trade_list=[]
    span_increment_seconds=1
    timestamp=time.timestamp()
    end_time=timestamp+span_increment_seconds
    cumulative_size=0
    while cumulative_size<=target_depth:
        trade_list=pd.DataFrame(exchange.publicGetMarketsMarketNameTrades(
            {'market_name':symbol,'start_time':timestamp,'end_time':end_time}
        )['result'],dtype=float)
        end_time = end_time + span_increment_seconds
        if not trade_list.empty: cumulative_size=(trade_list['size']*trade_list['price']).sum()
    trade_list['time'] = trade_list['time'].apply(lambda t: dateutil.parser.isoparse(t).timestamp())
    trade_list.sort_values('time',inplace=True,ascending=True)

    ## then compute avg over cumsum>depth
    trade_array=pd.DataFrame()
    trade_array['time']=((trade_list['time']-timestamp)*trade_list['size']*trade_list['price']).cumsum()
    trade_array['price']=(trade_list['price']*trade_list['size']*trade_list['price']).cumsum()
    trade_array['size'] = (trade_list['size']*trade_list['price']).cumsum()

    interpolator=trade_array.set_index('size')[['price','time']]
    interpolator.loc[target_depth]=np.NaN
    interpolator.loc[0]=pd.Series({'time':0,'size':0,'price':trade_array.loc[0,'price']})
    interpolator.interpolate(method='index',inplace=True)

    avg_time=interpolator.loc[target_depth,'time'] / target_depth
    avg_price = interpolator.loc[target_depth, 'price'] / target_depth

    return (avg_price,avg_time)

def mkt_speed(exchange, symbol, target_depth=10000):
    # side='bids' or 'asks'
    # returns time taken to trade a certain target_depth (in USD)
    nowtime = datetime.now(tz=timezone.utc)
    trades=pd.DataFrame(exchange.fetch_trades(symbol))
    if trades.shape[0] == 0: return 9999999
    nowtime = nowtime + timedelta(microseconds=int(0.5*(nowtime.microsecond-datetime.now().microsecond))) ### to have unbiased ref

    trades['size']=(trades['price']*trades['amount']).cumsum()

    interpolator=trades.set_index('size')['timestamp']
    interpolator[float(target_depth)]=np.NaN
    interpolator.interpolate(method='index',inplace=True)
    res=interpolator[float(target_depth)]
    return nowtime-datetime.fromtimestamp(res / 1000, tz=timezone.utc)

def fetch_ohlcv(self, symbol, timeframe='1m', start=None, end=None, params={}):
    self.load_markets()
    market, marketId = self.get_market_params(symbol, 'market_name', params)
    request = {
        'resolution': self.timeframes[timeframe],
        'market_name': marketId,
        'start_time':int(start),
        'end_time':int(end)
    }
    response = self.publicGetMarketsMarketNameCandles(self.extend(request, params))
    result = self.safe_value(response, 'result', [])
    return self.parse_ohlcvs(result, market, timeframe, int(start)*1000, 1501)

def fetch_spot_or_perp(self, symbol, point_in_time, params={}):
    if symbol=='USD/USD': return 1
    symbol = symbol.replace('_LOCKED','')
    self.load_markets()
    market, marketId = self.get_market_params(symbol, 'market_name', params)
    if not market is None:
        result=fetch_ohlcv(self,symbol, timeframe='15s', start=point_in_time, end=point_in_time+15, params=params)
        return result[0][1]
    else:
        perp_symbol = symbol.split('/')[0] + '-PERP'
        try:
            result= fetch_ohlcv(self,perp_symbol, timeframe='15s', start=point_in_time, end=point_in_time+15, params=params)
        except: return None
        return result[0][1]

def fetch_my_borrows(exchange,coin,params={}):
    request = {
        'market': coin,
    }
    response = exchange.private_get_spot_margin_market_info(exchange.extend(request, params))
    return response['result']

def fetch_coin_details(exchange):
    coin_details=pd.DataFrame(exchange.publicGetWalletCoins()['result']).astype(dtype={'collateralWeight': 'float','indexPrice': 'float'}).set_index('id')

    borrow_rates = pd.DataFrame(exchange.private_get_spot_margin_borrow_rates()['result']).astype(dtype={'coin': 'str', 'estimate': 'float', 'previous': 'float'}).set_index('coin')[['estimate']]
    borrow_rates[['estimate']]*=24*365.25
    borrow_rates.rename(columns={'estimate':'borrow'},inplace=True)

    lending_rates = pd.DataFrame(exchange.private_get_spot_margin_lending_rates()['result']).astype(dtype={'coin': 'str', 'estimate': 'float', 'previous': 'float'}).set_index('coin')[['estimate']]
    lending_rates[['estimate']] *= 24 * 365.25
    lending_rates.rename(columns={'estimate': 'lend'}, inplace=True)

    borrow_volumes = pd.DataFrame(exchange.public_get_spot_margin_borrow_summary()['result']).astype(dtype={'coin': 'str', 'size': 'float'}).set_index('coin')
    borrow_volumes.rename(columns={'size': 'funding_volume'}, inplace=True)

    all= pd.concat([coin_details,borrow_rates,lending_rates,borrow_volumes],join='outer',axis=1)
    all.loc[coin_details['spotMargin'] == False,'borrow']= None ### hope this throws an error...
    all.loc[coin_details['spotMargin'] == False, 'lend'] = 0
    #all.drop(['name'],axis=1,inplace=True)### because future has name too

    return all

# time in mili, rate annualized, size 1h(?)
def fetch_borrow_rate_history(exchange, coin,start_time,end_time,params={}):
    request = {
        'coin': coin,
        'start_time': start_time,
        'end_time': end_time
    }

    try:
        response = exchange.publicGetSpotMarginHistory(exchange.extend(request, params))
    except:
        return pd.DataFrame()

    if len(exchange.safe_value(response, 'result', []))==0: return pd.DataFrame()
    result = pd.DataFrame(exchange.safe_value(response, 'result', [])).astype({'coin':str,'time':str,'size':float,'rate':float})
    result['time']=result['time'].apply(lambda t:dateutil.parser.isoparse(t).timestamp()*1000)
    result['rate']*=24*365.25
    result['size']*=24 # assume borrow size is for the hour

    return result

def fetch_funding_rate_history(exchange, perp,start_time,end_time,params={}):
    request = {
        'start_time': start_time,
        'end_time': end_time,
        'future': perp.name,
        'resolution': exchange.describe()['timeframes']['1h']}

    response = exchange.publicGetFundingRates(exchange.extend(request, params))

    if len(exchange.safe_value(response, 'result', []))==0: return pd.DataFrame()
    result = pd.DataFrame(exchange.safe_value(response, 'result', [])).astype({'future':str,'rate':float,'time':str})
    result['time']=result['time'].apply(lambda t:dateutil.parser.isoparse(t).timestamp()*1000)
    result['rate']*=24*365.25

    return result

def collateralWeightInitial(future):# TODO: API call to collateralWeight(Initial)
    if future['underlying'] in ['BUSD','FTT','HUSD','TUSD','USD','USDC','USDP','WUSDC']:
        return future['collateralWeight']
    elif future['underlying'] in ['AUD','BRL','BRZ','CAD','CHF','EUR','GBP','HKD','SGD','TRY','ZAR']:
        return future['collateralWeight']-0.01
    elif future['underlying'] in ['BTC','USDT','WBTC','WUSDT']:
        return future['collateralWeight']-0.025
    else:
        return future['collateralWeight']-0.05

### get all static fields
def fetch_futures(exchange,includeExpired=False,includeIndex=False,params={}):
    response = exchange.publicGetFutures(params)

    expired = exchange.publicGetExpiredFutures(params) if includeExpired==True else []
    coin_details = await fetch_coin_details(exchange)

    #### for IM calc
    account_leverage = exchange.privateGetAccount()['result']
    if float(account_leverage['leverage']) >= 50: print("margin rules not implemented for leverage >=50")
    dummy_size = 100000  ## IM is in ^3/2 not linear, but rule typically kicks in at a few M for optimal leverage of 20 so we linearize

    markets = exchange.safe_value(response, 'result', []) + exchange.safe_value(expired, 'result', [])
    result = []
    for i in range(0, len(markets)):
        market = markets[i]
        underlying = exchange.safe_string(market, 'underlying')
        mark = exchange.safe_number(market, 'mark')
        imfFactor = exchange.safe_number(market, 'imfFactor')

        ## eg ADA has no coin details
        if not underlying in coin_details.index:
            if not includeIndex: continue

        result.append({
            'ask': exchange.safe_number(market, 'ask'),
            'bid': exchange.safe_number(market, 'bid'),
            'change1h': exchange.safe_number(market, 'change1h'),
            'change24h': exchange.safe_number(market, 'change24h'),
            'changeBod': exchange.safe_number(market, 'changeBod'),
            'volumeUsd24h': exchange.safe_number(market, 'volumeUsd24h'),
            'volume': exchange.safe_number(market, 'volume'),
            'symbol': exchange.safe_string(market, 'name'),
            "enabled": exchange.safe_value(market, 'enabled'),
            "expired": exchange.safe_value(market, 'expired'),
            "expiry": exchange.safe_string(market, 'expiry') if exchange.safe_string(market, 'expiry') else 'None',
            'index': exchange.safe_number(market, 'index'),
            'imfFactor': exchange.safe_number(market, 'imfFactor'),
            'last': exchange.safe_number(market, 'last'),
            'lowerBound': exchange.safe_number(market, 'lowerBound'),
            'mark': exchange.safe_number(market, 'mark'),
            'name': exchange.safe_string(market, 'name'),
            "perpetual": exchange.safe_value(market, 'perpetual'),
            'positionLimitWeight': exchange.safe_value(market, 'positionLimitWeight'),
            "postOnly": exchange.safe_value(market, 'postOnly'),
            'priceIncrement': exchange.safe_value(market, 'priceIncrement'),
            'sizeIncrement': exchange.safe_value(market, 'sizeIncrement'),
            'underlying': exchange.safe_string(market, 'underlying'),
            'upperBound': exchange.safe_value(market, 'upperBound'),
            'type': exchange.safe_string(market, 'type'),
         ### additionnals
            'account_leverage': float(account_leverage['leverage']),
            'collateralWeight':coin_details.loc[underlying,'collateralWeight'] if not includeIndex else 'coin_details not found',
            'underlyingType': getUnderlyingType(coin_details.loc[underlying]) if underlying in coin_details.index else 'index',
            'spot_ticker': exchange.safe_string(market, 'underlying')+'/USD',
            'spotMargin': coin_details.loc[underlying,'spotMargin'] if not includeIndex else 'coin_details not found',
            'tokenizedEquity':coin_details.loc[underlying,'tokenizedEquity'] if not includeIndex else 'coin_details not found',
            'usdFungible':coin_details.loc[underlying,'usdFungible'] if not includeIndex else 'coin_details not found',
            'fiat':coin_details.loc[underlying,'fiat'] if not includeIndex else 'coin_details not found',
            'expiryTime':dateutil.parser.isoparse(exchange.safe_string(market, 'expiry')).replace(tzinfo=None)
                            if exchange.safe_string(market, 'type') == 'future' else np.NaN
        })
    return result

def fetch_latencyStats(exchange,days,subaccount_nickname):
    #stats = exchange.publicGetStatsLatencyStats({'days':days,'subaccount_nickname':subaccount_nickname})
    return []#stats['result']
