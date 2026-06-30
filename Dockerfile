FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 7860

CMD ["gunicorn", "--timeout", "120", "--bind", "0.0.0.0:7860", "app:app"]