FROM python:3.12-slim
WORKDIR /collector
COPY collector.py alerts.json ./
RUN mkdir /data && chown nobody /data  # a named volume keeps this owner, so the snapshot file can be written
USER nobody
EXPOSE 9090
CMD ["python", "collector.py"]
