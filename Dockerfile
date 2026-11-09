FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["sh", "-c", "exec streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port ${PORT:-8080} --server.headless true --server.enableCORS true --server.enableXsrfProtection true"]
