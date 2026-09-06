"""AWS entry point: no reload, no access logs containing financial URLs, explicit proxy trust."""
import logging
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
logging.basicConfig(level=logging.INFO, format='%(message)s')
if not os.getenv('FORWARDED_ALLOW_IPS') or os.getenv('FORWARDED_ALLOW_IPS') == '*':
    raise RuntimeError('Set FORWARDED_ALLOW_IPS to the trusted ALB subnet CIDRs')
if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, workers=int(os.getenv('WEB_WORKERS', '2')),
                access_log=False, proxy_headers=True,
                forwarded_allow_ips=os.environ['FORWARDED_ALLOW_IPS'],
                limit_concurrency=64, timeout_keep_alive=5, timeout_graceful_shutdown=30)
