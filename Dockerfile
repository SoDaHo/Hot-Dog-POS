FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Projektdateien (inkl. POS.py, *.html, icons, etc.)
COPY . /app/

# Gunicorn auf Port 8000
EXPOSE 8000
CMD ["gunicorn","-w","2","-k","gthread","-b","0.0.0.0:8000","POS:app"]