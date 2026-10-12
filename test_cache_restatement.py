"""Exercise actual cache functions without importing runtime secrets or starting a bot."""
import ast,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from math import isfinite,isclose
from unittest.mock import Mock
import pandas as pd

class RestatementTest(unittest.TestCase):
    def test_both_runtime_lines_refresh_or_preserve_atomic_history(self):
        for folder in ('QuantBotSystem','QuantBotSystem_Portfolio_Beta'):
            for incomplete in (False,True):
                with self.subTest(folder=folder,incomplete=incomplete):
                    path=Path(__file__).parent/folder/'core_market_data_store.py'
                    tree=ast.parse(path.read_text(encoding='utf-8-sig'))
                    names={'_coherent_refresh','_upsert_ohlcv'}
                    ns=dict(pd=pd,isfinite=isfinite,isclose=isclose,datetime=datetime,timezone=timezone)
                    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),str(path),'exec'),ns)
                    c=sqlite3.connect(':memory:')
                    try:
                        c.execute('CREATE TABLE ohlcv(ticker TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,adj_close REAL,volume REAL,updated_at TEXT,PRIMARY KEY(ticker,date))')
                        def frame(days,value):return pd.DataFrame({k:[value]*len(days) for k in ('Open','High','Low','Close','Adj Close','Volume')},index=[pd.Timestamp(d).date() for d in days])
                        ns['_upsert_ohlcv'](c,'TEST',frame(['2020-01-02','2020-01-10'],100.))
                        partial=frame(['2020-01-10','2020-01-13'],50.)
                        full=frame(['2020-01-02','2020-01-10','2020-01-13'],50.)
                        ns['_download_history_range']=Mock(return_value=partial if incomplete else full)
                        if incomplete:
                            with self.assertRaisesRegex(ValueError,'Incomplete'):ns['_coherent_refresh'](c,'TEST',partial)
                        else:ns['_upsert_ohlcv'](c,'TEST',ns['_coherent_refresh'](c,'TEST',partial))
                        self.assertEqual([r[0] for r in c.execute('SELECT close FROM ohlcv ORDER BY date')],[100.,100.] if incomplete else [50.,50.,50.])
                        ns['_download_history_range'].assert_called_once()
                        ns['_download_history_range'].reset_mock()
                        ns['_coherent_refresh'](c,'TEST',frame(['2020-01-10'],100. if incomplete else 50.))
                        ns['_download_history_range'].assert_not_called()
                    finally:c.close()
