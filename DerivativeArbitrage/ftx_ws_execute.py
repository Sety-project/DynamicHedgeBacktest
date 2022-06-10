from io_utils import *
from ftx_utils import *
from ftx_history import fetch_trades_history
from ftx_portfolio import diff_portoflio, MarginCalculator
import ccxtpro

# parameters guide
# 'max_nb_coins': 'sharding'
# 'time_budget': scaling aggressiveness to 0 at time_budget (in seconds)
# 'slice_factor': slice in % of request
# 'global_beta': other coin weight in the global risk
# 'max_cache_size': mkt data cache
# 'message_cache_size': missed messages cache
# 'entry_tolerance': green light if basket better than quantile(entry_tolerance)
# 'rush_tolerance': mkt on both legs if basket better than quantile(rush_tolerance)
# 'stdev_window': stdev horizon for scaling parameters. in sec.
# 'edit_price_tolerance': place on edit_price_tolerance (in minutes) *  stdev
# 'aggressive_edit_price_tolerance': in priceIncrement
# 'edit_trigger_tolerance': chase at edit_trigger_tolerance (in minutes) *  stdev
# 'stop_tolerance': stop at stop_tolerance (in minutes) *  stdev
# 'check_frequency': risk recon frequency. in seconds
# 'delta_limit': in % of pv

class CustomRLock(threading._PyRLock):
    @property
    def count(self):
        return self._count

def loop(func):
    @functools.wraps(func)
    async def wrapper_loop(*args, **kwargs):
        self=args[0]
        while len(args)==1 or (args[1] in args[0].running_symbols):
            try:
                value = await func(*args, **kwargs)
            except ccxt.NetworkError as e:
                self.myLogger.debug(str(e))
                self.myLogger.info('reconciling after '+func.__name__+' dropped off')
                await self.reconcile()
            except Exception as e:
                self.myLogger.warning(e, exc_info=True)
                raise e
    return wrapper_loop

def symbol_locked(wrapped):
    '''decorates self.lifecycle_xxx(lientOrderId) to prevent race condition on state'''
    @functools.wraps(wrapped)
    def _wrapper(*args, **kwargs):
        return wrapped(*args, **kwargs) # TODO:temporary disabled

        self=args[0]
        symbol = args[1]['clientOrderId'].split('_')[1]
        while self.lock[symbol].count:
            self.myLogger.warning(f'{self.lock[symbol].count}th race conditions on {symbol} blocking {wrapped.__name__}')
            #await asyncio.sleep(0)
        with self.lock[symbol]:
            try:
                return wrapped(*args, **kwargs)
            except KeyError as e:
                pass
            except Exception as e:
                pass
    return _wrapper

def intercept_message_during_reconciliation(wrapped):
    '''decorates self.watch_xxx(message) to block incoming messages during reconciliation'''
    @functools.wraps(wrapped)
    def _wrapper(*args, **kwargs):
        self=args[0]
        if args[0].lock['reconciling'].locked():
            self.myLogger.info(f'message during reconciliation{args[2]}')
            self.message_missed.append(args[2])
        else:
            return wrapped(*args, **kwargs)
    return _wrapper

class myFtx(ccxtpro.ftx):
    def __init__(self, parameters, config={}):
        super().__init__(config=config)
        self.parameters = parameters
        self.lock = {'reconciling':threading.Lock()}
        self.message_missed = collections.deque(maxlen=parameters['message_cache_size'])
        if __debug__: self.all_messages = collections.deque(maxlen=parameters['message_cache_size']*10)
        self.rest_semaphor = asyncio.Semaphore(safe_gather_limit)

        self.orders_lifecycle = dict()
        self.latest_order_reconcile_timestamp = None
        self.latest_fill_reconcile_timestamp = None
        self.latest_exec_parameters_reconcile_timestamp = None

        self.limit = myFtx.LimitBreached(parameters['check_frequency'])
        self.exec_parameters = {}
        self.running_symbols =[]

        self.risk_reconciliations = []
        self.risk_state = {}
        self.pv = None
        self.usd_balance = None # it's handy for marginal calcs
        self.margin_headroom = None
        self.margin_calculator = None
        self.calculated_IM = None

        self.myLogger = logging.getLogger(__name__)

    # --------------------------------------------------------------------------------------------
    # ---------------------------------- various helpers -----------------------------------------
    # --------------------------------------------------------------------------------------------

    class DoneDeal(Exception):
        def __init__(self,status):
            super().__init__(status)
    class TimeBudgetExpired(Exception):
        def __init__(self,status):
            super().__init__(status)
    class LimitBreached(Exception):
        def __init__(self,check_frequency,limit=None):
            super().__init__()
            self.limit = limit
            self.check_frequency = check_frequency

    def find_clientID_from_fill(self,fill):
        '''find order by id, even if still in flight
        all layers events must carry id if known !! '''
        if 'clientOrderId' in fill:
            found = fill['clientOrderId']
        else:
            try:
                found = next(clientID for clientID, events in self.orders_lifecycle.items() if
                             any('id' in event and event['id'] == fill['order'] for event in events))
            except StopIteration as e:# could still be in flight --> lookup
                try:
                    found = next(clientID for clientID,events in self.orders_lifecycle.items() if
                                 any(event['price'] == fill['price']
                                     and event['amount'] == fill['amount']
                                     and event['symbol'] == fill['symbol']
                                     #and x['type'] == fill['type'] # sometimes None in fill
                                     and event['side'] == fill['side'] for event in events))
                except StopIteration as e:
                    raise Exception("fill {} not found".format(fill))
        return found

    def mid(self,symbol):
        data = self.tickers[symbol] if symbol in self.tickers else self.markets[symbol]['info']
        return 0.5*(float(data['bid'])+float(data['ask']))

    def amount_to_precision(self, symbol, amount):
        market = self.market(symbol)
        return self.decimal_to_precision(amount, ccxt.ROUND, market['precision']['amount'], self.precisionMode, self.paddingMode)

    def build_logging(self):
        '''3 handlers: >=debug, ==info and >=warning'''
        class MyFilter(object):
            '''this is to restrict info logger to info only'''
            def __init__(self, level):
                self.__level = level

            def filter(self, logRecord):
                return logRecord.levelno <= self.__level

        # logs
        handler_warning = logging.FileHandler('Runtime/logs/ftx_ws_execute/warning.log', mode='w')
        handler_warning.setLevel(logging.WARNING)
        handler_warning.setFormatter(logging.Formatter(f"%(levelname)s: %(message)s"))
        self.myLogger.addHandler(handler_warning)

        handler_info = logging.FileHandler('Runtime/logs/ftx_ws_execute/info.log', mode='w')
        handler_info.setLevel(logging.INFO)
        handler_info.setFormatter(logging.Formatter(f"%(levelname)s: %(message)s"))
        handler_info.addFilter(MyFilter(logging.INFO))
        self.myLogger.addHandler(handler_info)

        handler_debug = logging.FileHandler('Runtime/logs/ftx_ws_execute/debug.log', mode='w')
        handler_debug.setLevel(logging.DEBUG)
        handler_debug.setFormatter(logging.Formatter(f"%(levelname)s: %(message)s"))
        self.myLogger.addHandler(handler_debug)

        handler_alert = logging.handlers.SMTPHandler(mailhost='smtp.google.com',
                                                     fromaddr='david@pronoia.link',
                                                     toaddrs=['david@pronoia.link'],
                                                     subject='auto alert',
                                                     credentials=('david@pronoia.link', ''),
                                                     secure=None)
        handler_alert.setLevel(logging.CRITICAL)
        handler_alert.setFormatter(logging.Formatter(f"%(levelname)s: %(message)s"))
        # self.myLogger.addHandler(handler_alert)

        self.myLogger.setLevel(logging.DEBUG)

    # --------------------------------------------------------------------------------------------
    # ---------------------------------- OMS             -----------------------------------------
    # --------------------------------------------------------------------------------------------

    # orders_lifecycle is a dictionary of blockchains, one per order intended.
    # each block has a lifecyle_state in ['pending_new','sent','pending_cancel','acknowledged','partially_filled','canceled','rejected','filled']
    # lifecycle_xxx do:
    # 1) resolve clientID
    # 2) validate block, notably maintaining orders_pending_new (which is orders_lifecycle[x][-1]['lifecycle_state']=='pending_new')
    # 3) build a new block from messages received
    # 4) mines it to the blockchain
    #
    allStates = set(['pending_new', 'pending_cancel', 'sent', 'cancel_sent', 'pending_replace', 'acknowledged', 'partially_filled', 'filled', 'canceled', 'rejected'])
    openStates = set(['pending_new', 'sent', 'pending_cancel', 'pending_replace', 'acknowledged', 'partially_filled'])
    acknowledgedStates = set(['acknowledged', 'partially_filled'])
    cancelableStates = set(['sent', 'acknowledged', 'partially_filled'])
    # --------------------------------------------------------------------------------------------

    def pending_new_histories(self, coin,symbol=None):
        '''returns all blockchains for risk group, which current state open_order_histories not filled or canceled'''
        if symbol:
            return [data
                    for clientID,data in self.orders_lifecycle.items()
                    if data[0]['symbol'] == symbol
                    and data[-1]['lifecycle_state'] == 'pending_new']
        else:
            return [data
                    for clientID,data in self.orders_lifecycle.items()
                    if data[0]['symbol'] in self.exec_parameters[coin].keys()
                    and data[-1]['lifecycle_state'] == 'pending_new']

    def filter_order_histories(self,symbol=None,state_set=None):
        '''returns all blockchains for symbol (all symbols if None), and current state'''
        return [data
                for data in self.orders_lifecycle.values()
                if (data[0]['symbol'] == symbol or symbol is None)
                and ((data[-1]['lifecycle_state'] in state_set) or state_set is None)]

    def latest_value(self,clientOrderId,key):
        for previous_state in reversed(self.orders_lifecycle[clientOrderId]):
            if key in previous_state:
                return previous_state[key]
        raise f'{key} not found for {clientOrderId}'

    def lifecycle_pending_new(self, order_event):
        '''self.orders_lifecycle = {clientId:[{key:data}]}
        order_event:trigger,symbol'''
        #1) resolve clientID
        nowtime = datetime.now().timestamp() * 1000
        clientOrderId = order_event['comment'] + '_' + order_event['symbol'] + '_' + str(int(nowtime))

        #2) validate block
        pass

        #3) make new block

        ## order details
        symbol = order_event['symbol']
        coin = self.markets[symbol]['base']

        eventID = clientOrderId + '_' + str(int(nowtime))
        current = {'clientOrderId':clientOrderId,
                   'eventID': eventID,
                   'lifecycle_state': 'pending_new',
                   'remote_timestamp': None,
                   'timestamp': nowtime,
                   'id': None} | order_event

        ## risk details
        risk_data = self.risk_state[coin]
        current |= {'risk_timestamp':risk_data[symbol]['delta_timestamp'],
                    'delta':risk_data[symbol]['delta'],
                    'netDelta': risk_data['netDelta'],
                    'pv(wrong timestamp)':self.pv,
                    'margin_headroom':self.margin_headroom,
                    'IM_discrepancy':self.calculated_IM - self.margin_headroom}

        ## mkt details
        if symbol in self.tickers:
            mkt_data = self.tickers[symbol]
            timestamp = mkt_data['timestamp']
        else:
            mkt_data = self.markets[symbol]['info']|{'bidVolume':0,'askVolume':0}#TODO: should have all risk group
            timestamp = self.exec_parameters['timestamp']
        current |= {'mkt_timestamp': timestamp} \
                   | {key: mkt_data[key] for key in ['bid', 'bidVolume', 'ask', 'askVolume']}

        #4) mine genesis block
        self.orders_lifecycle[clientOrderId] = [current]

        return clientOrderId

    @symbol_locked
    def lifecycle_sent(self, order_event):
        '''order_event:clientOrderId,timestamp,remaining,status'''
        # 1) resolve clientID
        clientOrderId = order_event['clientOrderId']

        # 2) validate block
        past = self.orders_lifecycle[clientOrderId][-1]
        if past['lifecycle_state'] not in ['pending_new','acknowledged','partially_filled']:
            self.myLogger.warning('order {} was {} before sent'.format(past['clientOrderId'],past['lifecycle_state']))
            return

        # 3) new block
        nowtime = datetime.now().timestamp() * 1000
        eventID = clientOrderId + '_' + str(int(nowtime))
        current = order_event | {'eventID':eventID,
                                 'lifecycle_state':'sent',
                                 'remote_timestamp':order_event['timestamp']}
        current['timestamp'] = nowtime

        if order_event['status'] == 'closed':
            if order_event['remaining'] == 0:
                current['order'] = order_event['id']
                current['id'] = None
                assert 'amount' in current,"'amount' in current"
                self.lifecycle_fill(current)
            else:
                current['lifecycle_state'] = 'rejected'
                self.lifecycle_cancel_or_reject(current)

        #4) mine
        else:
            self.orders_lifecycle[clientOrderId] += [current]

    @symbol_locked
    def lifecycle_pending_cancel(self, order_event):
        '''this is first called with result={}, then with the rest response. could be two states...
        order_event:clientOrderId,status,trigger'''
        # 1) resolve clientID
        clientOrderId = order_event['clientOrderId']

        # 2) validate block
        past = self.orders_lifecycle[clientOrderId][-1]
        if past['lifecycle_state'] not in self.openStates:
            self.myLogger.warning('order {} re-canceled'.format(past['clientOrderId']))
            return

        # 3) new block
        nowtime = datetime.now().timestamp() * 1000
        eventID = clientOrderId + '_' + str(int(nowtime))
        current = order_event | {'eventID': eventID,
                                 'remote_timestamp': None,
                                 'lifecycle_state': 'pending_cancel'}
        current['timestamp'] = nowtime

        # 4) mine
        self.orders_lifecycle[clientOrderId] += [current]

    @symbol_locked
    def lifecycle_cancel_sent(self, order_event):
        '''this is first called with result={}, then with the rest response. could be two states...
        order_event:clientOrderId,status'''
        # 1) resolve clientID
        clientOrderId = order_event['clientOrderId']

        # 2) validate block
        past = self.orders_lifecycle[clientOrderId][-1]
        if not past['lifecycle_state'] in self.openStates:
            self.myLogger.warning('order {} re-canceled'.format(past['clientOrderId']))
            return

        # 3) new block
        nowtime = datetime.now().timestamp() * 1000
        eventID = clientOrderId + '_' + str(int(nowtime))
        current = order_event | {'eventID': eventID,
                                 'lifecycle_state': 'cancel_sent',
                                 'remote_timestamp': None,
                                 'timestamp': nowtime}

        # 4) mine
        self.orders_lifecycle[clientOrderId] += [current]

    @symbol_locked
    def lifecycle_acknowledgment(self, order_event):
        '''order_event: clientOrderId, trigger,timestamp,status'''
        # 1) resolve clientID
        clientOrderId = order_event['clientOrderId']

        # 2) validate block...is this needed?
        pass

        # 3) new block
        nowtime = datetime.now().timestamp()*1000
        eventID = clientOrderId + '_' + str(int(nowtime))
        current = order_event | {'eventID': eventID,
                                 'remote_timestamp': order_event['timestamp']}
        # overwrite some fields
        current['timestamp'] = nowtime

        if order_event['status'] in ['new', 'open', 'triggered']:
            current['lifecycle_state'] = 'acknowledged'
            self.orders_lifecycle[clientOrderId] += [current]
        elif order_event['status'] in ['closed']:
            #TODO: order event. not necessarily rejected ?
            if order_event['remaining'] == 0:
                current['order'] = order_event['id']
                current['id'] = None
                self.lifecycle_fill(current)
            else:
                current['lifecycle_state'] = 'rejected'
                self.lifecycle_cancel_or_reject(current)
        elif order_event['status'] in ['canceled']:
            current['lifecycle_state'] = 'canceled'
            self.lifecycle_cancel_or_reject(current)
        # elif order_event['filled'] !=0:
        #     raise Exception('filled ?')#TODO: code that ?
        #     current['order'] = order_event['id']
        #     current['id'] = None
        #     assert 'amount' in current,"assert 'amount' in current"
        #     self.lifecycle_fill(current)
        else:
            raise Exception('unknown status{}'.format(order_event['status']))

    @symbol_locked
    def lifecycle_fill(self,order_event):
        '''order_event: id, trigger,timestamp,amount,order,symbol'''
        # 1) resolve clientID
        clientOrderId = self.find_clientID_from_fill(order_event)
        symbol = order_event['symbol']
        coin = self.markets[symbol]['base']
        size_threhold = self.exec_parameters[coin][symbol]['sizeIncrement'] / 2

        # reconcile often rereads trades, skip then. There is no way to spot dupes from acknowledge though :(
        if order_event['id'] in [block['fillId']
                                     for block in self.orders_lifecycle[clientOrderId]
                                     if 'fillId' in block]:
            return
        else:# 3) new block
            nowtime = datetime.now().timestamp()*1000
            eventID = clientOrderId + '_' + str(int(nowtime))
            current = order_event | {'eventID':eventID,
                                     'fillId': order_event['id'],
                                     'remote_timestamp': order_event['timestamp']}
            # overwrite some fields
            current['timestamp'] = nowtime
            current['remaining'] = max(0, self.latest_value(clientOrderId,'remaining') - order_event['amount'])
            current['id'] = order_event['order']

            if current['remaining'] < size_threhold:
                self.orders_lifecycle[clientOrderId] += [current|{'lifecycle_state':'filled'}]
            else:
                self.orders_lifecycle[clientOrderId] += [current | {'lifecycle_state': 'partially_filled'}]

    @symbol_locked
    def lifecycle_cancel_or_reject(self, order_event):
        '''order_event: id, trigger,timestamp,amount,order,symbol'''
        # 1) resolve clientID
        clientOrderId = order_event['clientOrderId']

        # 2) validate block
        pass

        # 3) new block
        # if isinstance(order_event['timestamp']+.1, float):
        #     nowtime = order_event['timestamp']
        # else:
        #     nowtime = order_event['timestamp'][0]
        nowtime = datetime.now().timestamp() * 1000
        eventID = clientOrderId + '_' + str(int(nowtime))
        current = order_event | {'eventID':eventID,
                                 'lifecycle_state': order_event['lifecycle_state'],
                                 'remote_timestamp': order_event['timestamp'] if 'timestamp' in order_event else None}
        current['timestamp'] = nowtime

        # 4) mine
        self.orders_lifecycle[clientOrderId] += [current]

    async def lifecycle_to_json(self,filename = 'Runtime/logs/ftx_ws_execute/latest_events.json'):
        '''asyncronous + has it's own lock'''
        lock = threading.Lock()
        with lock:
            async with aiofiles.open(filename,mode='w') as file:
                await file.write(json.dumps(self.orders_lifecycle, cls=NpEncoder))
            shutil.copy2(filename, 'Runtime/logs/ftx_ws_execute/'+datetime.utcfromtimestamp(self.exec_parameters['timestamp']/1000).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d-%H-%M")+'_events.json')

    async def risk_reconciliation_to_json(self,filename = 'Runtime/logs/ftx_ws_execute/latest_risk_reconciliations.json'):
        '''asyncronous + has it's own lock'''
        lock = threading.Lock()
        with lock:
            async with aiofiles.open(filename,mode='w') as file:
                await file.write(json.dumps(self.risk_reconciliations, cls=NpEncoder))
            shutil.copy2(filename, 'Runtime/logs/ftx_ws_execute/'+datetime.utcfromtimestamp(self.exec_parameters['timestamp']/1000).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d-%H-%M")+'_risk_reconciliations.json')

    #@synchronized
    async def reconcile_fills(self):
        '''fetch fills, to recover missed messages'''
        sincetime = max(self.exec_parameters['timestamp'],self.latest_fill_reconcile_timestamp-1000)
        fetched_fills = await self.fetch_my_trades(since=sincetime)
        for fill in fetched_fills:
            fill['comment'] = 'reconciled'
            fill['clientOrderId'] = self.find_clientID_from_fill(fill)
            self.lifecycle_fill(fill)
        self.latest_fill_reconcile_timestamp = datetime.now(timezone.utc).timestamp()*1000
        # hopefully using another lock instance
        await self.lifecycle_to_json()

    async def reconcile_orders(self):
        self.latest_order_reconcile_timestamp = datetime.now(timezone.utc).timestamp() * 1000

        internal_order_internal_status = {clientOrderId:data[-1] for clientOrderId,data in self.orders_lifecycle.items()
                         if data[-1]['lifecycle_state'] in myFtx.openStates}
        internal_order_external_status = await safe_gather([self.fetch_order(id=None, params={'clientOrderId':clientOrderId})
                                                   for clientOrderId in internal_order_internal_status.keys()])
        external_orders = await self.fetch_open_orders()

        # missing orders
        for order in external_orders:
            if order['clientOrderId'] not in internal_order_internal_status.keys():
                if order['clientOrderId'] in self.orders_lifecycle.keys():
                    self.lifecycle_acknowledgment(order | {'comment':'reconciled_missing'})
                    self.myLogger.warning('{} was missing'.format(order['clientOrderId']))
                else:
                    raise Exception('{} never intended'.format(order['clientOrderId']))

        #zombie orders
        for external_status in internal_order_external_status:
            if external_status['status'] != 'open':
                self.lifecycle_cancel_or_reject(external_status | {'lifecycle_state': 'canceled', 'comment':'reconciled_zombie'})
                self.myLogger.warning('{} was a {} zombie'.format(external_status['clientOrderId'],internal_order_internal_status[external_status['clientOrderId']]))

        await self.lifecycle_to_json()

    # ---------------------------------------------------------------------------------------------
    # ---------------------------------- PMS -----------------------------------------
    # ---------------------------------------------------------------------------------------------

    async def build_state(self, weights,parameters): # cut in 10):
        '''initialize all state and does some filtering (weeds out slow underlyings; should be in strategy)
            target_sub_portfolios = {coin:{rush_level_increment,
            symbol1:{'spot_price','diff','target'}]}]'''
        self.build_logging()

        self.limit.delta_limit = self.parameters['delta_limit']

        frequency = timedelta(minutes=1)
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=self.parameters['stdev_window'])

        trades_history_list = await safe_gather([fetch_trades_history(
            self.market(symbol)['id'], self, start, end, frequency=frequency)
            for symbol in weights['name']],semaphore=self.rest_semaphor)
        trading_fees = await self.fetch_trading_fees()

        weights['diffCoin'] = weights['optimalCoin'] - weights['currentCoin']
        weights['diffUSD'] = weights['diffCoin'] * weights['spot_price']
        weights['name'] = weights['name'].apply(lambda s: self.market(s)['symbol'])
        weights.set_index('name', inplace=True)
        coin_list = weights['coin'].unique()

        # {coin:{symbol1:{data1,data2...},sumbol2:...}}
        full_dict = {coin:
                         {data['symbol']:
                              {'diff': weights.loc[data['symbol'], 'diffCoin'],
                               'target': weights.loc[data['symbol'], 'optimalCoin'],
                               'spot_price': weights.loc[data['symbol'], 'spot_price'],
                               'volume': data['volume'].mean().squeeze() / frequency.total_seconds(),# in coin per second
                               'series': data['vwap']}
                          for data in trades_history_list if data['coin']==coin}
                     for coin in coin_list}

        # exclude coins too slow or symbol diff too small, limit to max_nb_coins
        diff_threshold = sorted(
            [max([np.abs(weights.loc[data['symbol'], 'diffUSD'])
                  for data in trades_history_list if data['coin']==coin])
             for coin in coin_list])[-min(self.parameters['max_nb_coins'],len(coin_list))]

        data_dict = {coin: coin_data
                     for coin, coin_data in full_dict.items() if
                     all(data['volume'] * self.parameters['time_budget'] > np.abs(data['diff']) for data in coin_data.values())
                     and any(np.abs(data['diff']) >= max(diff_threshold/data['spot_price'], float(self.markets[symbol]['info']['minProvideSize'])) for symbol, data in coin_data.items())}
        if data_dict =={}:
            self.exec_parameters = {'timestamp': end.timestamp() * 1000}
            raise myFtx.DoneDeal('nothing to do')

        self.running_symbols = [symbol
                                    for coin_data in data_dict.values()
                                    for symbol in coin_data.keys()]

        # get times series of target baskets, compute quantile of increments and add to last price
        # remove series
        self.exec_parameters = {'timestamp':end.timestamp()*1000} \
                               |{sys.intern(coin):
                                     {sys.intern(symbol):
                                         {
                                             sys.intern('diff'): data['diff'],
                                             sys.intern('target'): data['target'],
                                             sys.intern('slice_size'): max([float(self.markets[symbol]['info']['minProvideSize']),
                                                                            self.parameters['slice_factor'] * np.abs(data['diff'])]),  # in coin
                                             sys.intern('priceIncrement'): float(self.markets[symbol]['info']['priceIncrement']),
                                             sys.intern('sizeIncrement'): float(self.markets[symbol]['info']['minProvideSize']),
                                             sys.intern('takerVsMakerFee'): trading_fees[symbol]['taker']-trading_fees[symbol]['maker'],
                                             sys.intern('spot'): self.mid(symbol),
                                             sys.intern('history'): collections.deque(maxlen=self.parameters['max_cache_size'])
                                         }
                                         for symbol, data in coin_data.items()}
                                 for coin, coin_data in data_dict.items()}

        self.lock |= {symbol: CustomRLock() for symbol in self.running_symbols}

        self.risk_state = {sys.intern(coin):
                               {sys.intern('netDelta'):0}
                               | {sys.intern(symbol):
                                   {
                                       sys.intern('delta'): 0,
                                       sys.intern('delta_timestamp'):end.timestamp()*1000,
                                       sys.intern('delta_id'): None
                                   }
                                   for symbol, data in coin_data.items()}
                           for coin, coin_data in data_dict.items()}

        futures = pd.DataFrame(await fetch_futures(self))
        account_leverage = float(futures.iloc[0]['account_leverage'])
        collateralWeight = futures.set_index('underlying')['collateralWeight'].to_dict()
        imfFactor = futures.set_index('new_symbol')['imfFactor'].to_dict()
        self.margin_calculator = MarginCalculator(account_leverage, collateralWeight, imfFactor)

        # populates risk, pv and IM
        self.latest_order_reconcile_timestamp = self.exec_parameters['timestamp']
        self.latest_fill_reconcile_timestamp = self.exec_parameters['timestamp']
        self.latest_exec_parameters_reconcile_timestamp = self.exec_parameters['timestamp']
        await self.reconcile()

        with open('Runtime/logs/ftx_ws_execute/latest_request.json', 'w') as file:
            json.dump({'inception_time':self.exec_parameters['timestamp']}|
                      {symbol:data
                       for coin,coin_data in self.exec_parameters.items() if coin in self.currencies
                       for symbol,data in coin_data.items() if symbol in self.markets}, file, cls=NpEncoder)
        shutil.copy2('Runtime/logs/ftx_ws_execute/latest_request.json','Runtime/logs/ftx_ws_execute/' + datetime.utcfromtimestamp(self.exec_parameters['timestamp'] / 1000).replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%d-%H-%M") + '_request.json')

    async def update_exec_parameters(self): # cut in 10):
        '''scales order placement params with time'''
        frequency = timedelta(minutes=1)
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=self.parameters['stdev_window'])

        # TODO: rather use cached history except for first run
        #if any(len(self.exec_parameters[self.markets[symbol]['base']][symbol]['history']) < 100 for symbol in self.running_symbols):
        trades_history_list = await safe_gather([fetch_trades_history(
            self.market(symbol)['id'], self, start, end, frequency=frequency)
            for symbol in self.running_symbols],semaphore=self.rest_semaphor)
        trades_history_dict = dict(zip(self.running_symbols, trades_history_list))

        coin_list = set([self.markets[symbol]['base'] for symbol in self.running_symbols])
        trades_history_dict = {coin:
                                   {symbol: trades_history_dict[symbol]
                                    for symbol in self.running_symbols
                                    if self.markets[symbol]['base'] == coin}
                               for coin in coin_list}

        # get times series of target baskets, compute quantile of increments and add to last price
        nowtime = datetime.now().timestamp() * 1000
        time_limit = self.exec_parameters['timestamp'] + self.parameters['time_budget']*1000

        if nowtime > time_limit:
            self.myLogger.info('time budget expired')
            raise myFtx.TimeBudgetExpired('')

        def basket_vwap_quantile(series_list, diff_list, quantile):
            series = pd.concat([serie * coef for serie, coef in zip(series_list, diff_list)], join='inner', axis=1)
            return series.sum(axis=1).quantile(quantile)

        scaler = max(0,(time_limit-nowtime)/(time_limit-self.exec_parameters['timestamp']))
        for coin,coin_data in trades_history_dict.items():
            self.exec_parameters[coin]['entry_level'] = basket_vwap_quantile(
                [data['vwap'] for data in coin_data.values()],
                [self.exec_parameters[coin][symbol]['diff'] for symbol in coin_data.keys()],
                self.parameters['entry_tolerance'])
            self.exec_parameters[coin]['rush_level'] = basket_vwap_quantile(
                [data['vwap'] for data in coin_data.values()],
                [self.exec_parameters[coin][symbol]['diff'] for symbol in coin_data.keys()],
                self.parameters['rush_tolerance'])
            for symbol, data in coin_data.items():
                stdev = data['vwap'].std().squeeze()
                if not (stdev>0): stdev = 1e-16
                self.exec_parameters[coin][symbol]['edit_trigger_depth'] = stdev * np.sqrt(self.parameters['edit_trigger_tolerance']) * scaler
                self.exec_parameters[coin][symbol]['edit_price_depth'] = stdev * np.sqrt(self.parameters['edit_price_tolerance']) * scaler
                self.exec_parameters[coin][symbol]['aggressive_edit_price_depth'] = self.parameters['aggressive_edit_price_tolerance'] * self.exec_parameters[coin][symbol]['priceIncrement']
                self.exec_parameters[coin][symbol]['stop_depth'] = stdev * np.sqrt(self.parameters['stop_tolerance']) * scaler

        self.latest_exec_parameters_reconcile_timestamp = nowtime
        self.myLogger.info(f'scaled params by {scaler} at {nowtime}')

    async def reconcile(self):
        '''update risk using rest
        all symbols not present when state is built are ignored !
        if some tickers are not initialized, it just uses markets
        trigger interception of all incoming messages until done'''

        # if already running, skip reconciliation
        if self.lock['reconciling'].locked():
            return

        # or reconcile, and lock until done
        with self.lock['reconciling']:
            risks = await safe_gather([self.fetch_positions(),self.fetch_balance()],semaphore=self.rest_semaphor)
            positions = risks[0]
            balances = risks[1]
            risk_timestamp = datetime.now().timestamp() * 1000

            # delta is noisy for perps, so override to delta 1.
            for position in positions:
                if float(position['info']['size'])!=0:
                    symbol = position['symbol']
                    coin = self.markets[symbol]['base']
                    if coin in self.risk_state and symbol in self.risk_state[coin]:
                        self.risk_state[coin][symbol]['delta'] = position['notional']*(1 if position['side'] == 'long' else -1) \
                            if self.markets[symbol]['type'] == 'future' \
                            else float(position['info']['size']) * self.mid(symbol)*(1 if position['side'] == 'long' else -1)
                        self.risk_state[coin][symbol]['delta_timestamp']=risk_timestamp

            for coin, balance in balances.items():
                if coin in self.currencies.keys() and coin != 'USD' and balance['total']!=0 and coin in self.risk_state and coin+'/USD' in self.risk_state[coin]:
                    symbol = coin+'/USD'
                    self.risk_state[coin][symbol]['delta'] = balance['total'] * self.mid(symbol)
                    self.risk_state[coin][symbol]['delta_timestamp'] = risk_timestamp

            for coin,coin_data in self.risk_state.items():
                coin_data['netDelta']= sum([data['delta'] for symbol,data in coin_data.items() if symbol in self.markets and 'delta' in data.keys()])

            # update pv and usd balance
            self.usd_balance = balances['USD']['total']
            self.pv = sum(coin_data[coin+'/USD']['delta'] for coin, coin_data in self.risk_state.items() if coin+'/USD' in coin_data.keys()) + self.usd_balance

            #compute IM
            spot_weight={}
            future_weight={}
            for position in positions:
                if float(position['info']['netSize'])!=0:
                    symbol = position['symbol']
                    mid = self.mid(symbol)
                    future_weight |= {symbol: {'weight': float(position['info']['netSize'])*mid, 'mark':mid}}
            for coin,balance in balances.items():
                if coin!='USD' and coin in self.currencies and balance['total']!=0:
                    mid = self.mid(coin+'/USD')
                    spot_weight |= {(coin):{'weight':balance['total']*mid,'mark':mid}}
            (spot_weight,future_weight) = MarginCalculator.add_pending_orders(self, spot_weight, future_weight)

            self.margin_calculator.estimate(self.usd_balance, spot_weight, future_weight)
            self.calculated_IM = sum(value for value in self.margin_calculator.estimated_IM.values())
            # fetch IM
            account_info = (await self.privateGetAccount())['result']
            self.margin_calculator.actual(account_info)
            self.margin_headroom = self.margin_calculator.actual_IM if float(account_info['totalPositionSize']) else self.pv

            await self.reconcile_fills()
            await self.reconcile_orders()
            await self.update_exec_parameters()

        # critical job is done, release lock

        # replay missed _messages. It's all concurrent so no pb with handle_xxx still filling the deque.
        self.myLogger.info(f'replaying {len(self.message_missed)} messages after recon')
        while self.message_missed:
            message = self.message_missed.popleft()
            data = self.safe_value(message, 'data')
            channel = message['channel']
            if channel == 'fills':
                fill = self.parse_trade(data)
                self.process_fill(fill | {'orderTrigger':'replayed'})
            elif channel == 'orders':
                order = self.parse_order(data)
                self.process_order(order | {'orderTrigger':'replayed'})
            # we record the orderbook but don't launch quoter (don't want to react after the fact)
            elif channel == 'orderbook':
                pass
                #orderbook = self.parse_order_book(data,symbol,data['time'])
                #self.process_order_book(symbol, orderbook | {'orderTrigger':'replayed'})

        # log risk
        if True:
            self.risk_reconciliations += [{'lifecycle_state': 'remote_risk', 'symbol':symbol_, 'delta_timestamp':self.risk_state[coin][symbol_]['delta_timestamp'],
                                           'delta':self.risk_state[coin][symbol_]['delta'],'netDelta':self.risk_state[coin]['netDelta'],
                                           'pv':self.pv,'calculated_IM':self.calculated_IM,'margin_headroom':self.margin_headroom}
                                          for coin,coindata in self.risk_state.items()
                                          for symbol_ in coindata.keys() if symbol_ in self.markets]
            await self.risk_reconciliation_to_json()

    # --------------------------------------------------------------------------------------------
    # ---------------------------------- WS loops, processors and message handlers ---------------
    # --------------------------------------------------------------------------------------------

    # ---------------------------------- orderbook_update

    @loop
    async def monitor_order_book(self, symbol):
        '''on each top of book update, update market_state and send orders
        tunes aggressiveness according to risk.
        populates event_records, maintains pending_new
        no lock is done, so we keep collecting mktdata'''
        orderbook = await self.watch_order_book(symbol)
        self.process_order_book(symbol, orderbook)

    def populate_ticker(self,symbol,orderbook):
        coin = self.markets[symbol]['base']
        timestamp = orderbook['timestamp'] * 1000
        mid = 0.5 * (orderbook['bids'][0][0] + orderbook['asks'][0][0])
        self.tickers[symbol] = {'symbol': symbol,
                                'timestamp': timestamp,
                                'bid': orderbook['bids'][0][0],
                                'ask': orderbook['asks'][0][0],
                                'mid': 0.5 * (orderbook['bids'][0][0] + orderbook['asks'][0][0]),
                                'bidVolume': orderbook['bids'][0][1],
                                'askVolume': orderbook['asks'][0][1]}
        self.exec_parameters[coin][symbol]['history'].append([timestamp, mid])

    def process_order_book(self, symbol, orderbook):
        if not self.lock['reconciling'].locked():
            with self.lock[symbol]:
                previous_mid = self.mid(symbol)  # based on ticker, in general
                self.populate_ticker(symbol, orderbook)

                # don't waste time on deep updates
                if self.mid(symbol) != previous_mid:
                    self.quoter(symbol, orderbook)

    # ---------------------------------- fills

    @loop
    async def monitor_fills(self):
        '''maintains risk_state, event_records, logger.info
            #     await self.reconcile_state() is safer but slower. we have monitor_risk to reconcile'''
        fills = await self.watch_my_trades()
        for fill in fills:
            self.process_fill(fill)

    def process_fill(self, fill):
        symbol = fill['symbol']
        coin = self.markets[symbol]['base']

        # update risk_state
        data = self.risk_state[coin][symbol]
        fill_size = fill['amount'] * (1 if fill['side'] == 'buy' else -1) * fill['price']
        data['delta'] += fill_size
        data['delta_timestamp'] = fill['timestamp']
        latest_delta = data['delta_id']
        data['delta_id'] = max(latest_delta or 0, int(fill['order']))
        self.risk_state[coin]['netDelta'] += fill_size

        # log event
        fill['clientOrderId'] = fill['info']['clientOrderId']
        fill['comment'] = 'websocket_fill'
        self.lifecycle_fill(fill)

        # logger.info
        self.myLogger.info('{} fill at {}: {} {} {} at {}'.format(symbol,
                                                                  fill['timestamp'],
                                                                  fill['side'], fill['amount'], symbol,
                                                                  fill['price']))

        current = self.risk_state[coin][symbol]['delta']
        initial = self.exec_parameters[coin][symbol]['target'] * fill['price'] - \
                  self.exec_parameters[coin][symbol][
                      'diff'] * fill['price']
        target = self.exec_parameters[coin][symbol]['target'] * fill['price']
        self.myLogger.info('{} risk at {} ms: {}% done [current {}, initial {}, target {}]'.format(
            symbol,
            self.risk_state[coin][symbol]['delta_timestamp'],
            (current - initial) / (target - initial) * 100,
            current,
            initial,
            target))

    # ---------------------------------- orders

    def process_order(self,order):
        assert order['clientOrderId'],"assert order['clientOrderId']"
        self.lifecycle_acknowledgment(order| {'comment': 'websocket_acknowledgment'}) # status new, triggered, open or canceled

    @loop
    async def monitor_orders(self):
        '''maintains orders, pending_new, event_records'''
        orders = await self.watch_orders()
        for order in orders:
            self.process_order(order)

    # ---------------------------------- misc

    @loop
    async def monitor_risk(self):
        '''redundant minutely risk check'''#TODO: would be cool if this could interupt other threads and restart it when margin is ok.
        await asyncio.sleep(self.limit.check_frequency)
        await self.reconcile()

        self.limit.limit = self.pv * self.limit.delta_limit
        absolute_risk = sum(abs(data['netDelta']) for data in self.risk_state.values())
        if absolute_risk > self.limit.limit:
            self.myLogger.warning(f'absolute_risk {absolute_risk} > {self.limit.limit}')
        if self.margin_headroom < self.pv/100:
            self.myLogger.warning(f'IM {self.margin_headroom}  < 1%')

    async def watch_ticker(self, symbol, params={}):
        '''watch_order_book is faster than watch_tickers so we DON'T LISTEN TO TICKERS. Dirty...'''
        raise Exception("watch_order_book is faster than watch_tickers so we DON'T LISTEN TO TICKERS. Dirty...")

    # ---------------------------------- just to record messages while reconciling
    @intercept_message_during_reconciliation
    def handle_my_trade(self, client, message):
        super().handle_my_trade(client, message)

    @intercept_message_during_reconciliation
    def handle_order(self, client, message):
        super().handle_order(client, message)

    #@intercept_message_during_reconciliation
    def handle_order_book_update(self, client, message):
        super().handle_order_book_update(client, message)

    # --------------------------------------------------------------------------------------------
    # ---------------------------------- order placement -----------------------------------------
    # --------------------------------------------------------------------------------------------

    # ---------------------------------- high level

    def quoter(self, symbol, orderbook):
        coin = self.markets[symbol]['base']
        mid = self.tickers[symbol]['mid']
        params = self.exec_parameters[coin][symbol]

        # size to do: aim at target, slice, round to sizeIncrement
        size = params['target'] - self.risk_state[coin][symbol]['delta']/mid
        # size < slice_size and margin_headroom
        size = np.sign(size)*float(self.amount_to_precision(symbol, min([np.abs(size), params['slice_size']])))
        if (np.abs(size) < self.exec_parameters[coin][symbol]['sizeIncrement']):
            self.running_symbols.remove(symbol)
            self.myLogger.info(f'{symbol} done, {self.running_symbols} left to do')
            if self.running_symbols == []:
                raise myFtx.DoneDeal('all done')
            else: return

        #risk
        delta_timestamp = self.risk_state[coin][symbol]['delta_timestamp']
        globalDelta = sum(self.risk_state[coin]['netDelta']  * (1 if _coin == coin else self.parameters['global_beta'])
                          for _coin in self.risk_state.keys()) / mid
        marginal_risk = np.abs(globalDelta + size)-np.abs(globalDelta)

        # if not enough margin, hold it# TODO: this may be slow, and depends on orders anyway
        #if self.margin_calculator.margin_cost(coin, mid, size, self.usd_balance) > self.margin_headroom:
        #    await asyncio.sleep(60) # TODO: I don't know how to stop/restart a thread..
        #    self.myLogger.info('margin {} too small for order size {}'.format(size*mid, self.margin_headroom))
        #    return None

        # if increases risk, go passive
        if marginal_risk>0:
            stop_depth = None
            current_basket_price = sum(self.mid(symbol)*params['diff']
                                       for symbol in self.exec_parameters[coin].keys() if symbol in self.markets)
            # mkt order if target reached.
            if current_basket_price + np.abs(params['diff']) * params['takerVsMakerFee']< self.exec_parameters[coin]['rush_level']:
                # (np.abs(netDelta+size)-np.abs(netDelta))/np.abs(size)*params['edit_price_depth'] # equate: profit if done ~ marginal risk * stdev
                edit_price_depth = 0
                for _symbol in self.exec_parameters[coin]:
                    if _symbol in self.markets:
                        self.exec_parameters[coin][_symbol]['edit_price_depth'] = 0
            # limit order if level is acceptable
            elif current_basket_price < self.exec_parameters[coin]['entry_level']:
                edit_price_depth = params['edit_price_depth']
            # hold off if level is bad
            else:
                return
        # if decrease risk, go aggressive
        else:
            edit_price_depth = params['aggressive_edit_price_depth']
            stop_depth = params['stop_depth']
        self.peg_or_stopout(symbol,size,orderbook,edit_trigger_depth=params['edit_trigger_depth'],edit_price_depth=edit_price_depth,stop_depth=stop_depth)

    def peg_or_stopout(self,symbol,size,orderbook,edit_trigger_depth,edit_price_depth,stop_depth=None):
        '''creates of edit orders, pegging to orderbook
        goes taker when depth<increment
        size in coin, already filtered
        skips if any pending_new, cancels duplicates
        extensive exception handling
        '''
        coin = self.markets[symbol]['base']

        #TODO: https://help.ftx.com/hc/en-us/articles/360052595091-Ratelimits-on-FTX
        opposite_side = self.tickers[symbol]['ask' if size>0 else 'bid']
        mid = self.tickers[symbol]['mid']

        priceIncrement = self.exec_parameters[coin][symbol]['priceIncrement']
        sizeIncrement = self.exec_parameters[coin][symbol]['sizeIncrement']

        if stop_depth is not None:
            stop_trigger = float(self.price_to_precision(symbol,stop_depth))
        edit_trigger = float(self.price_to_precision(symbol,edit_trigger_depth))
        edit_price = float(self.price_to_precision(symbol,opposite_side - (1 if size>0 else -1)*edit_price_depth))

        try:
            # remove open order dupes is any (shouldn't happen)
            event_histories = self.filter_order_histories(symbol,self.openStates)
            if len(event_histories) > 1:
                first_pending_new = np.argmin(np.array([data[0]['timestamp'] for data in event_histories]))
                for i,event_history in enumerate(self.filter_order_histories(symbol,self.cancelableStates)):
                    if i != first_pending_new:
                        order = event_histories[i][-1]
                        asyncio.create_task(self.cancel_order(event_history[-1]['clientOrderId'],'duplicates'))
                        self.myLogger.warning('canceled duplicate {} order {}'.format(symbol,event_history[-1]['clientOrderId']))


            # if no open order, create genesis
            order_side = 'buy' if size>0 else 'sell'
            if len(event_histories)==0:
                (postOnly, ioc) = (True, False) if edit_price_depth > priceIncrement else (False, True)
                asyncio.create_task(self.create_order(symbol, 'limit', order_side, np.abs(size), price=edit_price,
                                                      params={'postOnly': postOnly,'ioc': ioc, 'comment':'new'}))
            # if only one and it's editable, stopout or peg or wait
            elif (self.latest_value(event_histories[0][-1]['clientOrderId'],'remaining') >= sizeIncrement) \
                    and event_histories[0][-1]['lifecycle_state'] in self.acknowledgedStates:
                order = event_histories[0][-1]
                order_distance = (1 if order['side'] == 'buy' else -1) * (opposite_side - order['price'])

                # panic stop. we could rather place a trailing stop: more robust to latency, but less generic.
                if stop_depth and order_distance > stop_trigger:
                    size = self.latest_value(order['clientOrderId'],'remaining')
                    price = mkt_at_size(orderbook, order_side, size)['side']
                    asyncio.create_task( self.edit_order(symbol, 'limit', order_side, size=size, price = price,
                                                         params={'ioc':True,'comment':'stop'},previous_clientOrderId = order['clientOrderId']))
                # peg limit order
                elif order_distance > edit_trigger and (np.abs(edit_price-order['price']) >= priceIncrement):
                    asyncio.create_task(self.edit_order(symbol, 'limit', order_side, np.abs(size),price=edit_price,
                                                        params={'postOnly': True,'comment':'chase'},previous_clientOrderId = order['clientOrderId']))

        ### see error_hierarchy in DerivativeArbitrage/venv/Lib/site-packages/ccxt/base/errors.py
        except ccxt.InsufficientFunds as e: # is ExchangeError
            cost = self.margin_calculator.margin_cost(coin, mid, size, self.usd_balance)
            self.myLogger.warning(f'marginal cost {cost}, vs margin_headroom {self.margin_headroom} and calculated_IM {self.calculated_IM}')
        except ccxt.InvalidOrder as e: # is ExchangeError
            if "Order not found" in str(e):
                self.myLogger.warning(str(e) + str(order))
            elif ("Size too small for provide" or "Size too small") in str(e):
                # usually because filled btw remaining checked and order modify
                self.myLogger.warning('{}: {} too small {}...or {} < {}'.format(order,np.abs(size),sizeIncrement,order['clientOrderId'].split('_')[2],order['timestamp']))
            else:
                self.myLogger.warning(str(e) + str(order))
        except ccxt.ExchangeError as e:  # is base error
            if "Must modify either price or size" in str(e):
                self.myLogger.warning(str(e) + str(order))
            else:
                self.myLogger.warning(str(e), exc_info=True)

    # ---------------------------------- low level

    async def edit_order(self,*args,**kwargs):
        if await self.cancel_order(kwargs.pop('previous_clientOrderId'), 'edit'):
            return await self.create_order(*args,**kwargs)

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        '''if acknowledged, place order. otherwise just reconcile
        orders_pending_new is blocking'''
        order = None
        coin = self.markets[symbol]['base']
        if self.pending_new_histories(coin) != []:#TODO: rather incorporate orders_pending_new in risk, rather than block
            if self.pending_new_histories(coin,symbol) != []:
                self.myLogger.debug('orders {} should not be in flight'.format([order['clientOrderId'] for order in self.pending_new_histories(coin,symbol)[-1]]))
            else:
                self.myLogger.debug('orders {} still in flight. holding off {}'.format(
                    [order['clientOrderId'] for order in self.pending_new_histories(coin)[-1]],symbol))
            await asyncio.sleep(1)
            #await self.reconcile()
        else:
            # set pending_new -> send rest -> if success, leave pending_new and give id. Pls note it may have been caught by handle_order by then.
            clientOrderId = self.lifecycle_pending_new({'symbol': symbol,
                                                        'type': type,
                                                        'side': side,
                                                        'amount': amount,
                                                        'remaining': amount,
                                                        'price': price,
                                                        'comment': params['comment']})
            try:
                order = await super().create_order(symbol, type, side, amount, price, params | {'clientOrderId':clientOrderId})
            except ccxt.InsufficientFunds as e:
                order = {'clientOrderId':clientOrderId,
                         'timestamp':datetime.now().timestamp() * 1000,
                         'lifecycle_state':'rejected',
                         'comment':'create/'+str(e)}
                self.lifecycle_cancel_or_reject(order)
            except Exception as e:
                order = {'clientOrderId':clientOrderId,
                         'timestamp':datetime.now().timestamp() * 1000,
                         'lifecycle_state':'rejected',
                         'comment':'create/'+str(e)}
                self.lifecycle_cancel_or_reject(order)
            else:
                self.lifecycle_sent(order)

        return order

    async def cancel_order(self, clientOrderId, trigger):
        '''set in flight, send cancel, set as pending cancel, set as canceled or insist'''
        symbol = clientOrderId.split('_')[1]
        self.lifecycle_pending_cancel({'clientOrderId':clientOrderId,
                                       'symbol': symbol,
                                       'comment':trigger})

        try:
            status = await super().cancel_order(None,params={'clientOrderId':clientOrderId})
            self.lifecycle_cancel_sent({'clientOrderId':clientOrderId,
                                        'symbol':symbol,
                                        'status':status,
                                        'comment':trigger})
            return True
        except ccxt.CancelPending as e:
            self.lifecycle_cancel_sent({'clientOrderId': clientOrderId,
                                        'symbol': symbol,
                                        'status': str(e),
                                        'comment': trigger})
            return True
        except ccxt.InvalidOrder as e: # could be in flight, or unknown
            self.lifecycle_cancel_or_reject({'clientOrderId':clientOrderId,
                                             'status':str(e),
                                             'lifecycle_state':'canceled',
                                             'comment':trigger})
            return False
        except Exception as e:
            self.myLogger.warning('cancel for {} fails with {}'.format(clientOrderId,str(e)))
            await asyncio.sleep(.1)
            return await self.cancel_order(clientOrderId, trigger+'+')

    async def close_dust(self):
        '''leaves slightly positive cash for ease of convert'''
        risks = await safe_gather([self.fetch_positions(),self.fetch_balance()],semaphore=self.rest_semaphor)
        positions = risks[0]
        balances = risks[1]

        futures_dust = [position for position in positions
                        if float(position['info']['size'])!=0
                        and np.abs(float(position['info']['size']))<2*float(self.markets[position['symbol']]['info']['minProvideSize'])]
        for position in futures_dust:
            details = {'symbol': position['symbol'],
                       'type': 'market',
                       'side': 'buy' if position['side'] == 'short' else 'sell',
                       'amount': float(position['info']['size'])}
            clientOrderId = self.lifecycle_pending_new(details|{'remaining': details['amount'],
                                                'comment': 'dust'})
            await super(ccxtpro.ftx,self).create_order(**details,params={'clientOrderId':clientOrderId})
        await asyncio.sleep(5)

        shorts_dust = {coin:float(self.markets[coin+'/USD']['info']['sizeIncrement']) for coin, balance in balances.items()
                       if coin in set(self.currencies) - set(['USD'])
                       and float(balance['total'])<0
                       and float(balance['total'])+float(self.markets[coin+'/USD']['info']['sizeIncrement'])>0}
        for coin,size in shorts_dust.items():
            details = {'symbol': coin+'/USD',
                       'type': 'market',
                       'side': 'buy',
                       'amount': size}
            clientOrderId = self.lifecycle_pending_new(details|{'remaining': details['amount'],
                                                'comment': 'dust'})
            await super(ccxtpro.ftx,self).create_order(**details,params={'clientOrderId':clientOrderId})

async def ftx_ws_spread_main_wrapper(*argv,**kwargs):
    allDone = False
    while not allDone:
        try:
            with open('Runtime/configs/tradeexecutor_params.json', 'r') as file:
                parameters = json.load(file)

            exchange = myFtx(parameters, config={  ## David personnal
                'enableRateLimit': True,
                'apiKey': 'ZUWyqADqpXYFBjzzCQeUTSsxBZaMHeufPFgWYgQU',
                'secret': api_params.loc['ftx', 'value'],
                'newUpdates': True})
            exchange.verbose = False
            exchange.headers = {'FTX-SUBACCOUNT': argv[2]}
            exchange.authenticate()
            await exchange.load_markets()

            if argv[0]=='sysperp':
                future_weights = pd.read_excel('Runtime/ApprovedRuns/current_weights.xlsx')
                target_portfolio = await diff_portoflio(exchange, future_weights)
                if target_portfolio.empty: return

            elif argv[0]=='spread':
                coin=argv[3]
                cash_name = coin+'/USD'
                future_name = coin + '-PERP'
                cash_price = float(exchange.market(cash_name)['info']['price'])
                future_price = float(exchange.market(future_name)['info']['price'])
                target_portfolio = pd.DataFrame(columns=['coin','name','optimalCoin','currentCoin','spot_price'],data=[
                    [coin,cash_name,float(argv[4])/cash_price,0,cash_price],
                    [coin,future_name,-float(argv[4])/future_price,0,future_price]])
                if target_portfolio.empty: return

            elif argv[0]=='flatten': # only works for basket with 2 symbols
                future_weights = pd.DataFrame(columns=['name','optimalWeight'])
                diff = await diff_portoflio(exchange, future_weights)
                smallest_risk = diff.groupby(by='coin')['currentCoin'].agg(lambda series: series.apply(np.abs).min() if series.shape[0]>1 else 0)
                target_portfolio=diff
                target_portfolio['optimalCoin'] = diff.apply(lambda f: smallest_risk[f['coin']]*np.sign(f['currentCoin']),axis=1)
                if target_portfolio.empty: return

            elif argv[0]=='unwind':
                future_weights = pd.DataFrame(columns=['name','optimalWeight'])
                target_portfolio = await diff_portoflio(exchange, future_weights)
                if target_portfolio.empty: return

            else:
                exchange.logger.exception(f'unknown command {argv[0]}',exc_info=True)
                raise Exception(f'unknown command {argv[0]}',exc_info=True)

            await exchange.build_state(target_portfolio, parameters)
            await safe_gather([exchange.monitor_risk(),exchange.monitor_orders(),exchange.monitor_fills()]+
                                   [exchange.monitor_order_book(symbol)
                                    for symbol in exchange.running_symbols],semaphore=exchange.rest_semaphor)

        except myFtx.DoneDeal as e:
            allDone = True
        except myFtx.TimeBudgetExpired as e:
            allDone = True
        except Exception as e:
            exchange.logger.warning(e,exc_info=True)
            raise e
        finally:
            await exchange.cancel_all_orders()
            await exchange.close_dust()
            await exchange.close()
            exchange.logger.info('exchange closed',exc_info=True)

    return

def ftx_ws_spread_main(*argv):
    argv=list(argv)
    if len(argv) == 0:
        argv.extend(['sysperp'])
    if len(argv) < 3:
        argv.extend(['ftx', 'SysPerp'])
    logging.info(f'running {argv}')
    if argv[0] in ['sysperp', 'flatten', 'unwind', 'spread']:
        return asyncio.run(ftx_ws_spread_main_wrapper(*argv))
        log_reader()
    else:
        logging.info(f'commands: sysperp [ftx][debug], flatten [ftx][debug],unwind [ftx][debug], spread [ftx][debug][coin][cash in usd]')

if __name__ == "__main__":
    ftx_ws_spread_main(*sys.argv[1:])