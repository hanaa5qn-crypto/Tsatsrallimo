import os


bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "*"
