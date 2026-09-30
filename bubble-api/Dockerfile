FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Внутри контейнера слушаем все интерфейсы; наружу порт публикуется только на 127.0.0.1 (docker-compose.yml)
ENV BUBBLE_API_HOST=0.0.0.0
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
