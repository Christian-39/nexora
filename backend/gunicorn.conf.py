import os
bind='0.0.0.0:8000'
workers=int(os.getenv('WEB_CONCURRENCY','3'))
worker_tmp_dir='/dev/shm'
timeout=int(os.getenv('GUNICORN_TIMEOUT','120'))
graceful_timeout=30
keepalive=5
accesslog='-'
errorlog='-'
capture_output=True
max_requests=2000
max_requests_jitter=200
forwarded_allow_ips=os.getenv('FORWARDED_ALLOW_IPS','127.0.0.1')
