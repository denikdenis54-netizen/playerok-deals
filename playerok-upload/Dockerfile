FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Бот работает постоянно, а веб-сервер нужен для Mini App.
# Render сам подставит PORT в окружение.
CMD ["sh", "-c", "python -m bot"]