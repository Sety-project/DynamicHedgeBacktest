from enum import Enum

import pandas as pd

config = pd.DataFrame(data={'id': [70916, 4610, 70510, 4623, 63048, 4627, 72202, 4656, 70374, 4670, 73714, 63021, 71426,
                                   62984],
                            'symbol': ['ADABUSD', 'ADAUSDT', 'BNBBUSD', 'BNBUSDT', 'BTCBUSD', 'BTCUSDT', 'DOGEBUSD',
                                       'DOGEUSDT', 'ETHBUSD', 'ETHUSDT', 'SOLBUSD', 'SOLUSDT', 'XRPBUSD', 'XRPUSDT'],
                            'tick_size': [0.0001, 0.0001, 0.01, 0.01, 0.1, 0.01, 0.00001, 0.00001, 0.01, 0.01, 0.001,
                                          0.001, 0.0001, 0.0001],
                            'step_size': [1, 1, 0.01, 0.01, 0.001, 0.001, 1, 1, 0.001, 0.001, 1, 1, 0.1, 0.1],
                            'minimum_order_size_base': [1, 1, 0.01, 0.01, 0.001, 0.001, 1, 1, 0.001, 0.001, 1, 1, 0.1,
                                                        0.1],
                            'minimum_order_size_quote': [5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5]})

source_directory: str = 'C:/Users/Tibor/Data/'


class Strategy(Enum):
    ASMM_PHI = 1
    ASMM_HIGH_LOW = 2
    ROLL_MODEL = 3


class Bar(Enum):
    ONE_SECOND = 1
    TEN_TICKS = 2


class AlphaModel(Enum):
    NONE = 0
    MOMENTUM = 1


class Direction(Enum):
    SHORT = -1
    NEUTRAL = 0
    LONG = 1
