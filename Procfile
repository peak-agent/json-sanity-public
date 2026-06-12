# multi-worker safe because server.py uses StreamableHTTPSessionManager(stateless=True)
web: gunicorn -w 4 -k uvicorn.workers.UvicornWorker server:app --bind 0.0.0.0:$PORT
