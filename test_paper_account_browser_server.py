"""Disposable browser fixture; never starts a trading runner or reads .env.

Used only by test_paper_account_browser.js. Its child is a lifecycle-lock holder
with the normal stop control channel, not a market-data/order execution loop.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def configure(root):
    os.environ.update(DELTA_DATA_DIR=str(root),DELTA_MODE='research',DELTA_EXECUTION_MODE='paper',
                      DELTA_LIVE_TRADING_ENABLED='false',DELTA_AUTO_BOT_LIVE_ENABLED='false',
                      DELTA_TELEGRAM_BOT_TOKEN='',DELTA_TELEGRAM_CHAT_ID='')
    from pydantic_settings import BaseSettings
    original=BaseSettings.__init__
    def no_dotenv(self,*args,**kwargs):
        kwargs['_env_file']=None
        original(self,*args,**kwargs)
    BaseSettings.__init__=no_dotenv


def child(root):
    configure(root)
    import live_bot as lb
    lock=lb.InstanceLock(root/'live_bot.lock','paper')
    channel=lb.ControlChannel(root/'bot_control')
    try:
        while True:
            for command in channel.pending():
                if command['cmd']=='stop':
                    channel.complete(command['id'],{'ok':True,'stopping':True,'pid':os.getpid()})
                    return
            time.sleep(.05)
    finally:lock.release()


def serve(port,empty):
    with tempfile.TemporaryDirectory(prefix='delta-paper-browser-') as scratch:
        root=Path(scratch);configure(root)
        import json
        import shutil
        from dataclasses import asdict,replace
        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        import uvicorn
        import requests
        def forbidden(*a,**k):raise AssertionError('External HTTP forbidden in browser fixture')
        requests.Session.request=forbidden
        import execution_engine as ee
        import bot_ops as ops
        project=Path(__file__).resolve().parent
        shutil.copytree(project/'data/metadata',root/'metadata')
        profile=replace(ee.FROZEN_PROFILES['BTCUSD'],history_start=None,rule_type='ema_pullback_scalp',
                        params={'fast_ema':9,'slow_ema':21},cost_gate=False,trend_filter_4h=False)
        ee.save_bot_profiles_raw({'BTCUSD':asdict(profile)})
        ops._signals=lambda:{}
        ops.set_active_mode('live')  # must never override the selected Paper screen
        process=None
        if not empty:
            clock=[int(time.time()*1e6)]
            store=ee.StateStore(ops.PATHS['db'],clock=lambda:clock[0])
            engine=ee.ExecutionEngine(store,'paper',profiles={'BTCUSD':profile},clock=lambda:clock[0])
            for _ in range(5):
                engine.manual_fill('BTCUSD',1,2,10000.,10,sl=9900.,tp=11000.)
                clock[0]+=60_000000;engine._paper_close('BTCUSD',9990.,'FIXTURE',clock[0])
            store.meta_set(utc_day_anchor_date=time.strftime('%Y-%m-%d',time.gmtime()),utc_day_start_equity_usd=50000/85)
            store.set_halt(True,'isolated five-trade fixture halt',clock[0]+86400_000000)
            store.conn.execute("INSERT INTO signal_evaluations(mode,symbol,bar_open_us,bar_close_us,evaluated_at_us,strategy,config_id,config_json,raw_signal,decision,detail) VALUES('paper','BTCUSD',1,2,3,'ema_pullback_scalp','fixture','{}',1,'skip','{}')")
            store.close()
            process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--child',scratch],
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
            for _ in range(100):
                if ops.runner()[0]:break
                time.sleep(.1)
            if not ops.runner()[0]:raise RuntimeError('Fixture process did not acquire lifecycle lock')
            ops._proc=process;ops._proc_bot_pid=process.pid
        app=FastAPI();app.include_router(ops.router)
        app.mount('/static',StaticFiles(directory=project/'static'),name='static')
        @app.get('/')
        def index():return FileResponse(project/'static/index.html')
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        @app.post('/__test/shutdown')
        def shutdown():
            server.should_exit=True
            return {'ok':True}
        try:server.run()
        finally:
            if process and process.poll() is None:
                from live_bot import ControlChannel
                ControlChannel(root/'bot_control').submit('stop')
                process.wait(timeout=10)


if __name__=='__main__':
    if sys.argv[1]=='--child':child(Path(sys.argv[2]))
    else:serve(int(sys.argv[1]),'--empty' in sys.argv)
