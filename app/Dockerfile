FROM python:3.12-slim
WORKDIR /app
COPY metrics.py service.py ./
USER nobody
EXPOSE 8000
CMD ["python", "service.py"]
