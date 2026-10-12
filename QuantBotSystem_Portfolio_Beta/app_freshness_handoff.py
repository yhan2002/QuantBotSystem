"""Existing bot scheduler handoff to the shared, bounded local price pipeline."""
import json,sqlite3,subprocess,sys
from contextlib import closing
from pathlib import Path
from datetime import datetime,timezone

CONFIG=Path(__file__).resolve().parents[1]/'operational_freshness/config.private.json'

def settings():
    if not CONFIG.is_file():raise RuntimeError('Shared freshness configuration missing; refresh/report blocked')
    value=json.loads(CONFIG.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or any(not isinstance(value.get(k),str) or not value[k].strip() for k in ('python','installation_root','state_root')):
        raise RuntimeError('Shared freshness configuration invalid; refresh/report blocked')
    return value

def refresh(label):
    config=settings()
    command=[config['python'],'-B',str(Path(config['installation_root'])/'freshness/runner.py'),'--config',str(CONFIG),'--reason','native_'+label]
    # The runner uses KillOnClose for its only child. This wait includes its cleanup.
    result=subprocess.run(command,timeout=config.get('execution_timeout_seconds',900)+20,capture_output=True,text=True)
    state=json.loads((Path(config['state_root'])/'state.json').read_text(encoding='utf-8'))
    return dict(status=state['status'],run_id=state.get('run_id'),returncode=result.returncode,
                last_success=state.get('last_success'),sources=state.get('sources',{}))

def validate(database,tickers):
    config=settings()
    sys.path.insert(0,str(Path(config['installation_root'])/'freshness'))
    try:
        from calendar_rules import completed_sessions,expected
        from worker import history,history_hash
        sessions=completed_sessions();home=Path(config['state_root'])
        state=json.loads((home/'state.json').read_text(encoding='utf-8')) if (home/'state.json').exists() else {}
        source=next((r for r in state.get('sources',{}).values() if Path(r['database']).resolve()==Path(database).resolve()),{})
        hashes=source.get('validated_history_sha256',{}) if source.get('status','').startswith('PROMOTED') else {}
        failures=[]
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as connection:
            connection.execute('BEGIN')
            for ticker in tickers:
                values=history(connection,'ohlcv',ticker)
                if not values or max(values)!=expected(ticker,sessions):failures.append({'ticker':ticker,'reason':'STALE_OR_MISSING'})
                elif ticker not in hashes or history_hash(values)!=hashes[ticker]:failures.append({'ticker':ticker,'reason':'UNVERIFIED_OR_CHANGED'})
        return dict(status='VALIDATED' if not failures else 'BLOCKED',failed_count=len(failures),failures=failures,run_id=state.get('run_id'))
    finally:sys.path.remove(str(Path(config['installation_root'])/'freshness'))
