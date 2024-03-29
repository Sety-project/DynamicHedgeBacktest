import sys,math,scipy,datetime,copy
import pandas as pd
import numpy as np
import requests
from datetime import *
from utils.blackscholes import black_scholes


class MktCurve:
    '''pd.Series mixin'''
    def __init__(self,timestamp,series):
        '''timsetamp in sec'''
        self.timestamp = timestamp
        self.series = series

    def interpolate(self,T):
        '''T in sec'''
        maturity_days = (T - self.timestamp) / 3600 / 24
        interpolator = scipy.interpolate.interp1d(self.series.index,
                                                  self.series.values,
                                                  fill_value="extrapolate")  # could do linear in fwd variance....
        return interpolator([maturity_days])[0]


class VolSurface:
    '''pd.DataFrame mixin
    somewhat approximate (uses atm to interpolate across delta)
    '''
    def __init__(self,timestamp,dataframe,fwdcurve):
        '''
        :param timestamp: in sec
        :param dataframe: delta x T
        :param fwdcurve: FwdCurve
        '''
        self.timestamp = timestamp
        self.dataframe = pd.DataFrame(dataframe)
        self.fwdcurve = fwdcurve
        self.atmcurve = MktCurve(self.timestamp,self.dataframe.loc[50])
    def interpolate(self,K,T):
        fwd = self.fwdcurve.interpolate(T)
        atm_vol = self.atmcurve.interpolate(T)
        approx_delta = black_scholes.delta(1 / fwd, K, atm_vol, T / 3600 / 24 / 365.25, 'P')
        return scipy.interpolate.RectBivariateSpline(self.dataframe.index, self.dataframe.columns, self.dataframe.values).ev(
            [np.clip(approx_delta * 100,a_min=min(self.dataframe.index),a_max=max(self.dataframe.index))],
            [np.clip(T / 3600 / 24,a_min=min(self.dataframe.columns),a_max=max(self.dataframe.columns))]
        )[0]  # TODO: could plug quantlib / heston
    def scale(self,scaler):
        '''in_place'''
        self.dataframe *= scaler
        return self

def deribit_smile_tardis(currency,whatever):
    '''not implemented. genesis volatility for now'''
    ## perp, volIndex...
    rest_history = deribit_history_main('just use',[currency],'deribit','cache')[0]

    ## tardis
    history = pd.read_csv(f"Runtime/Deribit_Mktdata_database/deribit_options_chain_2019-07-01_{currency}.csv", nrows=1e5)
    history.dropna(subset=['underlying_price','strike_price','local_timestamp','mark_iv','expiration'],inplace=True)
    #history['expiry'] = history['expiration'].apply(lambda t: pd.to_datetime(int(t), unit='us'))
    history['timestamp'] = history['timestamp'].apply(lambda t: pd.to_datetime(int(t), unit='us'))
    #history['otm_delta'] = history.apply(lambda d: (d['delta'] if d['delta']<0.5 else d['delta']-1) if d['type'] == 'call' else (d['delta'] if d['delta']>-0.5 else d['delta']+1),axis=1)
    history['stddev_factor'] = history.apply(
        lambda d: np.log(d['underlying_price']/d['strike_price'])/np.sqrt((d['expiration']-d['local_timestamp'])/1e6/3600/24/365.25), axis=1)

    #return history

    per_hour = history.groupby(pd.Grouper(key="timestamp", freq="1h"))
    ATM = pd.Series()
    for (hour, hour_data) in per_hour.__iter__():
        print(hour_data[['underlying_price', 'local_timestamp']].mean())
        s_t = hour_data[['underlying_price', 'local_timestamp']].mean()
        s = s_t['underlying_price']
        t = s_t['local_timestamp']

        # simple_ATM, later replace 0.25 by some vega amount floor
        ATM_data = hour_data[np.abs(hour_data['stddev_factor']) < 0.25]
        interpolator = ATM_data.set_index('stddev_factor')['mark_iv']
        interpolator.loc[0] = np.NaN
        interpolator.interpolate(method='index', inplace=True).sort_index()
        ATM[hour] = interpolator.loc[0]

        simple_ATM = dict()
        for side in ['bid', 'ask']:
            simple_ATM[side] = (ATM_data['mark_iv'] * \
                                ATM_data[side + '_amount'] * ATM_data['vega'] \
                                / (ATM_data[side + '_amount'] * ATM_data['vega']).sum()
                                ).sum(min_count=1)
            # vega*amount weighted regress (eg heston) of side_iv on otm_delta,expiry (later bid and ask)
            # vega*amount weighted regress (eg heston) of side_iv on otm_delta,expiry (later bid and ask)

def deribit_smile_genesisvolatility(currency,start = datetime.now(tz=timezone.utc) - timedelta(days=30)):
    '''
    full volsurface history as multiindex dataframe: date as milli x (tenor as years, strike of 'atm')
    '''

    # just read from file, since we only have 30d using LITE
    nrows = int(1 + (datetime.now(tz=timezone.utc) - start).total_seconds() / 3600)
    data = pd.read_excel('Runtime/Deribit_Mktdata_database/genesisvolatility/manual.xlsx',index_col=0,header=[0,1],sheet_name=currency,nrows=nrows)/100
    data.index = [t.replace(tzinfo=timezone.utc) for t in data.index]
    return data

    url = "https://app.pinkswantrading.com/graphql"
    headers = {
        'gvol-lite': api_params.loc['genesisvolatility','value'],
        'Content-Type': 'application/json',
        'accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    ''' 
    atm 
    '''
    payload = "{\"query\":\"query FixedMaturityAtmLite($exchange: ExchangeEnumType, $symbol:BTCOrETHEnumType)" \
              "{\\n  FixedMaturityAtm(exchange:$exchange, symbol: $symbol) " \
                "{\\n    date\\n    atm7\\n    atm30\\n    atm60\\n    atm90\\n    atm180\\n    currency\\n  }\\n}\"," \
              "\"variables\":{\"exchange\":\"deribit\",\"symbol\":\""+currency+"\"}}"
    response = requests.request("GET", url, headers=headers, data=payload).json()
    atm = pd.DataFrame(response['data']['FixedMaturityAtm'])
    atm['date'] = atm['date'].apply(lambda x: datetime.utcfromtimestamp(float(x) / 1000).replace(tzinfo=timezone.utc))
    atm.set_index('date',inplace=True)
    atm.columns = pd.MultiIndex.from_tuples([(float(c.split('atm')[1])/365.25,'atm') for c in atm.columns],names=['tenor','strike'])
    atm /= 100

    ''' 
    skew 
    '''
    payload = "{\"query\":\"query FixedMaturitySkewLite($exchange: ExchangeEnumType, $symbol:BTCOrETHEnumType)" \
              "{\\n  FixedMaturitySkewLite(exchange:$exchange, symbol: $symbol) {\\n    date\\n    currency\\n    " \
              "thirtyFiveDelta7DayExp\\n    twentyFiveDelta7DayExp\\n    fifteenDelta7DayExp\\n    fiveDelta7DayExp\\n" \
              "thirtyFiveDelta30DayExp\\n    twentyFiveDelta30DayExp\\n    fifteenDelta30DayExp\\n    fiveDelta30DayExp\\n" \
              "    thirtyFiveDelta60DayExp\\n    twentyFiveDelta60DayExp\\n    fifteenDelta60DayExp\\n    fiveDelta60DayExp\\n" \
              "    thirtyFiveDelta90DayExp\\n    twentyFiveDelta90DayExp\\n    fifteenDelta90DayExp\\n    fiveDelta90DayExp\\n" \
              "    thirtyFiveDelta180DayExp\\n    twentyFiveDelta180DayExp\\n    fifteenDelta180DayExp\\n    fiveDelta180DayExp\\n  } \\n}\"," \
              "\"variables\":{\"exchange\":\"deribit\",\"symbol\":\""+currency+"\"}}"
    response = requests.request("GET", url, headers=headers, data=payload).json()
    skew = pd.DataFrame(response['data']['FixedMaturitySkewLite'])
    skew['date'] = skew['date'].apply(lambda x: datetime.utcfromtimestamp(float(x) / 1000).replace(tzinfo=timezone.utc))
    skew.set_index('date',inplace=True)
    def columnParser(word):
        def word2float(word):
            if word == 'thirtyFive': return .35
            elif word == 'twentyFive': return .25
            elif word == 'fifteen': return .15
            elif word == 'five': return .05
            else: raise Exception('unknown word' + word)
        DeltaSeparated = word.split('Delta')
        return (float(DeltaSeparated[1].split('DayExp')[0])/365.25, word2float(DeltaSeparated[0]))

    skew.columns = pd.MultiIndex.from_tuples([columnParser(c) for c in skew.columns],names=['tenor','strike'])
    skew /= 100

    # full vol surface history
    data = atm.join(skew,how='outer')
    data = data[~data.duplicated()]
    # assert set(data.index.levels[1][1:]-data.index.levels[1][:-1]) == set([timedelta(hours=1)])

    for c in data.columns:
        if c[1]!='atm':
            data[(c[0],-c[1])] = data[(c[0], 'atm')] - 0.5 * data[(c[0], c[1])]
            data[(c[0],c[1])] = data[(c[0],'atm')] + 0.5 * data[(c[0],c[1])]

    output_path = 'Runtime/Deribit_Mktdata_database/genesisvolatility/surface_history.csv'
    previous_data = pd.read_csv(output_path).index
    data = data[~previous_data]
    data.to_csv(output_path,mode='a', header=not os.path.exists(output_path))
    shutil.copy2(output_path, 'Runtime/Deribit_Mktdata_database/genesisvolatility/surface_history_' + datetime.now(tz=timezone.utc).strftime("%Y-%m-%d-%Hh")+'.csv')

def deribit_smile_main(*argv):
    argv = list(argv)
    if len(argv) == 0:
        argv.extend(['genesisvolatility'])
    if len(argv) < 2:
        argv.extend(['ETH'])
    if len(argv) < 3:
        argv.extend([1])
    print(f'running {argv}')

    if argv[0] == 'genesisvolatility':
        deribit_smile_genesisvolatility(currency=argv[1])
    elif argv[0] == 'tardis':
        deribit_smile_tardis(argv[1],argv[2])
    else:
        raise Exception('unknown request ' + argv[0])

if __name__ == "__main__":
    deribit_smile_main(*sys.argv[1:])