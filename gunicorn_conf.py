import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"

# Tell uvicorn workers to trust the reverse proxy's forwarded headers so that
# request.client.host reflects the real client IP (used for rate limiting).
forwarded_allow_ips = "*"
