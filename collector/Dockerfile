FROM python:3.12-slim
WORKDIR /collector
COPY collector.py alerts.json ./
USER nobody
EXPOSE 9090
CMD ["python", "collector.py"]
