r"""Run PAPER regressions offline in disposable storage, without reading .env.

    .venv\Scripts\python.exe verify_paper_runtime.py
    .venv\Scripts\python.exe verify_paper_runtime.py --existing
"""
from contextlib import redirect_stderr, redirect_stdout
import argparse
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--existing', action='store_true', help='run offline Step 6, 9 and 18 checks instead of pytest')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='delta-paper-verification-') as scratch:
        os.environ.update(DELTA_DATA_DIR=scratch, DELTA_MODE='research', DELTA_EXECUTION_MODE='paper',
                          DELTA_LIVE_TRADING_ENABLED='false', DELTA_AUTO_BOT_LIVE_ENABLED='false',
                          DELTA_TELEGRAM_BOT_TOKEN='', DELTA_TELEGRAM_CHAT_ID='', PYTHONDONTWRITEBYTECODE='1')
        from pydantic_settings import BaseSettings
        original = BaseSettings.__init__
        def no_dotenv(self, *a, **kw):
            kw['_env_file'] = None
            original(self, *a, **kw)
        BaseSettings.__init__ = no_dotenv
        shutil.copytree(Path(__file__).parent/'data/metadata', Path(scratch)/'metadata')
        if not args.existing:
            import pytest
            return pytest.main(['-q','test_paper_runtime.py','test_paper_account_settings.py','test_bot_deploy.py','test_review_repairs.py',
                                'test_btc_paper_preset.py','test_paper_forward_btc.py','--tb=short'])
        import requests
        import httpx
        def forbidden(*a, **kw):
            raise AssertionError('external HTTP forbidden during offline verification')
        requests.Session.request = forbidden
        httpx.HTTPTransport.handle_request = forbidden
        httpx.AsyncHTTPTransport.handle_async_request = forbidden
        import importlib
        failed = False
        for name, options in [('step6_verify',['--skip-live','--skip-regression']),
                              ('step9_repair_verify',['--skip-regression']),
                              ('step18_strategy_lab_verify',[])]:
            output = io.StringIO()
            sys.argv = [name+'.py', *options]
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    module = importlib.import_module(name)
                    code = module.main()
            except Exception:
                import traceback
                traceback.print_exc(file=output)
                code = 1
            text = output.getvalue()
            print(f'{name}: exit={code}')
            if code:
                print('\n'.join(line for line in text.splitlines() if 'FAIL' in line or 'Error' in line or 'Traceback' in line))
                failed = True
            else:
                print('\n'.join(text.splitlines()[-3:]))
        return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
