FROM python:3.12-slim

WORKDIR /app

# Dépendances système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app/ .

# Streamlit config
RUN mkdir -p /root/.streamlit
RUN cat > /root/.streamlit/config.toml << 'EOF'
[server]
port = 8888
address = "0.0.0.0"
headless = true
enableCORS = false
enableXsrfProtection = false

[theme]
base = "dark"
primaryColor = "#4CAF50"
backgroundColor = "#0e1117"
secondaryBackgroundColor = "#1e2130"
textColor = "#fafafa"
EOF

EXPOSE 8888

CMD ["streamlit", "run", "app.py", "--server.port=8888", "--server.address=0.0.0.0"]
