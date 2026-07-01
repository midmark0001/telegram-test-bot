FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hf_processor.py .

EXPOSE 7860

CMD ["gunicorn", "--timeout", "300", "--bind", "0.0.0.0:7860", "hf_processor:app"]