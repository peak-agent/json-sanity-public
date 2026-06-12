FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No secrets required to start: billing runs in mock mode without
# STRIPE_SECRET_KEY and the Supabase client is created lazily.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "gunicorn -w 2 -k uvicorn.workers.UvicornWorker server:app --bind 0.0.0.0:${PORT}"]
