FROM python:3.12-slim

# Устанавливаем системные зависимости для gevent
RUN apt-get update && apt-get install -y libevent-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["gunicorn", "-k", "gevent", "-w", "1", "-b", "0.0.0.0:10000", "app:app"]