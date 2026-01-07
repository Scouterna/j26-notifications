FROM python:3.12-slim

ENV TZ="Europe/Stockholm"
WORKDIR /app

COPY src/requirements.txt ./src/requirements.txt
RUN pip install --no-cache-dir -r ./src/requirements.txt

COPY . .

ENV PYTHONPATH=/app/src

CMD ["python", "src/start.py"]
