
FROM python:3.11-slim

# System deps for Playwright and curl-impersonate
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 libxcomposite1 libxrandr2 \
    libxdamage1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libgbm1 libxfixes3 libxext6 libxi6 libxtst6 \
    libglib2.0-0 libgtk-3-0 libdrm2 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium)
RUN playwright install chromium

# Copy app
COPY . /app
WORKDIR /app

# Use the PORT provided by Render
ENV PORT=10000
EXPOSE 10000

# Start the server binding to the provided PORT environment variable
CMD ["sh", "-c", "hypercorn main:app --bind 0.0.0.0:${PORT}"]
