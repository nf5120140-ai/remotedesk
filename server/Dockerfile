FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
COPY web ./web
ENV PORT=8765 OWNERS_FILE=/data/owners.json
RUN mkdir -p /data
EXPOSE 8765
CMD ["python", "relay.py"]
