FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5051 \
    HOST=0.0.0.0

WORKDIR /app

# Tizim paketlarini yangilash
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Kutubxonalarni o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Loyiha fayllarini nusxalash
COPY . .

# Ma'lumotlar va zaxiralar papkalarini yaratish
RUN mkdir -p /app/data /app/zaxira_nusxalar

EXPOSE 5051

# Gunicorn orqali ishga tushirish (8 worker thread)
CMD ["gunicorn", "--bind", "0.0.0.0:5051", "--workers", "2", "--threads", "4", "--timeout", "120", "wsgi:application"]
