
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 libxcomposite1 libxrandr2 \
    libxdamage1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libgbm1 libxfixes3 libxext6 libxi6 libxtst6 \
    libglib2.0-0 libgtk-3-0 libdrm2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY . /app
WORKDIR /app

ENV PORT=10000
EXPOSE 10000

CMD ["hypercorn", "main:app", "--bind", "0.0.0.0:10000"]
