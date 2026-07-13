# File name: config.py
# Defines bot access permissions and default strategy universes.
BOT_TOKEN = "8937649776:AAEAT-JLR9A2Zw2F9B6Fmyv5hz_s_q2l-Dg" # @YHanScreenerBot
ALLOWED_CHAT_IDS = [
    "7485559455",
    "-5489984462",
    "6835580933",
    "6395561265",
    "5416647161"
]

universe_groups = {
    "CSQ_CP": [
        'ABNB', 'ADSK', 'AFRM', 'ALAB', 'AMAT', 'AMD', 'ANET', 'APP', 'ARM', 'AS', 
        'AVGO', 'AXON', 'BROS', 'CAH', 'CAT', 'CDNS', 'CEG', 'CMG', 'CRWD', 'CSCO', 
        'CVNA', 'DASH', 'DE', 'DHI', 'DUOL', 'EOG', 'ETN', 'FTNT', 'GD', 'GE', 
        'GEV', 'GRMN', 'HALO', 'HLT', 'HOOD', 'HWM', 'ISRG', 'KKR', 'KO', 'LEN', 
        'LLY', 'LMT', 'LNG', 'LYV', 'META', 'MO', 'MRK', 'NET', 'NFLX', 'NOC', 
        'NTRA', 'NVDA', 'ONON', 'PANW', 'PCAR', 'PGR', 'PLTR', 'PM', 'RCL', 'RDDT', 
        'RL', 'RTX', 'SE', 'SPOT', 'STT', 'T', 'TCOM', 'TEM', 'TLN', 'TMO', 
        'TMUS', 'TPG', 'TPR', 'TSLA', 'TTWO', 'UBER', 'VRSN', 'VRT', 'WMT', 'WYNN'
    ],
    "CSQ_MP": [
        'AMZN', 'APP', 'AS', 'DASH', 'DUOL', 'GS', 'HIMS', 'HOOD', 'INTC', 'META', 
        'MSTR', 'NET', 'NFLX', 'NVDA', 'PLTR', 'SPOT', 'TEM', 'TSLA'
    ],
    "CSQ_SMID": [
        'ACHR', 'ALAB', 'AMBA', 'ANET', 'APA', 'APH', 'ARWR', 'AVAV', 'BBIO', 'BKSY', 
        'BMNR', 'BWXT', 'CCJ', 'CEG', 'CLSK', 'COIN', 'CRCL', 'CRDO', 'CRWV', 'EH', 
        'EQT', 'ET', 'GEV', 'GH', 'GLNG', 'HESM', 'HOOD', 'HSAI', 'IONQ', 'IREN', 
        'JOBY', 'KMI', 'KTOS', 'LDOS', 'LEU', 'LNG', 'LUNR', 'MARA', 'MDGL', 'MSTR', 
        'NBIS', 'NOC', 'OKLO', 'PLTR', 'PONY', 'QBTS', 'RCAT', 'RDW', 'RGTI', 'RIOT', 
        'SMR', 'SOFI', 'STX', 'UEC', 'WMB', 'XYZ'
    ],
    "CSQ_UNIV": [
        'AAL', 'AAPL', 'ABNB', 'ADBE', 'AFRM', 'AGNC', 'ALNY', 'AMAT', 'AMD', 'AMZN', 
        'ANET', 'APP', 'APPF', 'ARM', 'AS', 'ASND', 'AUR', 'AVGO', 'BAC', 'BBIO', 
        'BKNG', 'BLK', 'BPOP', 'BRKR', 'BRO', 'C', 'CAH', 'CAT', 'CAVA', 'CB', 
        'CBSH', 'CCJ', 'CDNS', 'CEG', 'CFLT', 'CHKP', 'CHRD', 'CMG', 'CRDO', 'CRM', 
        'CRWD', 'CSCO', 'CSW', 'CVLT', 'CVNA', 'CYTK', 'CZR', 'DASH', 'DBX', 'EOG', 
        'ETN', 'FICO', 'FISV', 'FRPT', 'FSV', 'FTNT', 'GD', 'GE', 'GEV', 'GGAL', 
        'GH', 'GOOGL', 'GRAL', 'GRMN', 'GS', 'GTLB', 'HALO', 'HAS', 'HLNE', 'HLT', 
        'HOOD', 'HQY', 'HSIC', 'INTU', 'ISRG', 'JPM', 'KDP', 'KKR', 'KO', 'LECO', 
        'LEN', 'LLY', 'LMT', 'LNWO', 'LRCX', 'LSCC', 'MAR', 'MASI', 'MAT', 'MDB', 
        'MDGL', 'META', 'MKSI', 'MMYT', 'MNDY', 'MS', 'MSFT', 'MSI', 'MTSI', 'MU', 
        'NCLH', 'NFLX', 'NOC', 'NOW', 'NTNX', 'NTRA', 'NUVL', 'NVDA', 'ORCL', 'OTEX', 
        'PANW', 'PEGA', 'PEP', 'PGR', 'PLTR', 'PM', 'PNFP', 'PODD', 'RCL', 'REGN', 
        'RGEN', 'RGLD', 'RL', 'RVMD', 'SAIA', 'SAIC', 'SNPS', 'SOFI', 'SPT', 'SRE', 
        'STT', 'TCOM', 'TEM', 'TLN', 'TMO', 'TMUS', 'TPG', 'TPR', 'TRMB', 'TSLA', 
        'TTD', 'UBER', 'VERX', 'VRNS', 'VRT', 'VRTX', 'VTR', 'WFC', 'WMT', 'WTFC', 'Z', 'ZION'
    ],
    "18C": [
        '002371.SZ', '0100.HK', '1021.HK', '1347.HK', '1384.HK', '2228.HK', '2432.HK', '2513.HK', '2533.HK', '2670.HK', 
        '300308.SZ', '300502.SZ', '3625.HK', '3881.HK', '3986.HK', '6082.HK', '6636.HK', '6651.HK', '688256.SS', '0800.HK', 
        '9903.HK'
    ],
    "JP_UNIV": [
        '1802.T', '1812.T', '2269.T', '2413.T', '2503.T', '2579.T', '2802.T', '2809.T', '285A.T', '286A.T', 
        '2897.T', '2914.T', '3038.T', '3064.T', '3086.T', '3099.T', '3110.T', '3407.T', '3563.T', '3659.T', 
        '3769.T', '4062.T', '4183.T', '4188.T', '4307.T', '4452.T', '4478.T', '4480.T', '4612.T', '4689.T', 
        '4901.T', '4912.T', '5016.T', '5020.T', '5076.T', '5108.T', '5201.T', '5334.T', '5401.T', '5801.T', 
        '5803.T', '6146.T', '6268.T', '6301.T', '6305.T', '6361.T', '6383.T', '6479.T', '6503.T', '6525.T', 
        '6586.T', '6622.T', '6701.T', '6752.T', '6770.T', '6856.T', '6871.T', '6890.T', '6902.T', '6920.T', 
        '6965.T', '6981.T', '7011.T', '7012.T', '7201.T', '7202.T', '7211.T', '7220.T', '7259.T', '7267.T', 
        '7532.T', '7731.T', '7751.T', '7936.T', '7974.T', '8058.T', '8111.T', '8306.T', '8697.T', '8750.T', 
        '8801.T', '8802.T', '9009.T', '9020.T', '9201.T', '9432.T', '9508.T', '9697.T', '9706.T', '9766.T', 
        '9843.T', '9983.T', 'PYPL'
    ],
    "CH_UNIV": [
        '000938.SZ', '000977.SZ', '002049.SZ', '002230.SZ', '002371.SZ', '002410.SZ', '0020.HK', '0100.HK', '0241.HK', '0268.HK', 
        '0800.HK', '0981.HK', '1021.HK', '1024.HK', '1093.HK', '1099.HK', '1177.HK', '1211.HK', '1347.HK', '1357.HK', 
        '1384.HK', '1797.HK', '1801.HK', '1877.HK', '1929.HK', '2018.HK', '2196.HK', '2228.HK', '2268.HK', '2269.HK', 
        '2359.HK', '2382.HK', '241.HK', '268.HK', '300124.SZ', '300142.SZ', '300308.SZ', '300502.SZ', '300628.SZ', '300661.SZ', 
        '300676.SZ', '300682.SZ', '301165.SZ', '3690.HK', '3888.HK', '600276.SS', '600460.SS', '600570.SS', '600588.SS', '600845.SS', 
        '601127.SS', '603019.SS', '603501.SS', '603605.SS', '603893.SS', '6082.HK', '6160.HK', '6185.HK', '6618.HK', '6636.HK', 
        '6651.HK', '6690.HK', '6862.HK', '688008.SS', '688012.SS', '688041.SS', '688068.SS', '688072.SS', '688082.SS', '688111.SS', 
        '688213.SS', '688256.SS', '688363.SS', '688396.SS', '688608.SS', '6969.HK', '9618.HK', '9626.HK', '9660.HK', '981.HK', 
        '9863.HK', '9888.HK', '9969.HK', '9992.HK', '9999.HK', 'ATAT', 'BZ', 'CHA', 'EDU', 'FUTU', 'HSAI', 
        'LI', 'MNSO', 'PONY', 'QFIN', 'TAL', 'TCOM', 'TME', 'VIPS', 'WRD', 'XPEV', 'YMM', 'YUMC'
    ]
}

# Build the deduplicated master universe from all configured groups.
all_tickers = set()
for group in universe_groups.values():
    all_tickers.update(group)
universe = sorted(list(all_tickers))
