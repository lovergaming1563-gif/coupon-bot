FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram-bot/ ./telegram-bot/

EXPOSE 8080

CMD ["python", "telegram-bot/bot.py"]
