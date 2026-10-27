# All three services in one container, for hosts that run a single container (Hugging Face Spaces).
FROM python:3.12-slim
WORKDIR /srv
COPY app ./app
COPY collector ./collector
COPY dashboard ./dashboard
COPY start.sh .
RUN useradd -m -u 1000 user
USER 1000
ENV COLLECTOR_TARGETS=http://localhost:8000/metrics COLLECTOR_URL=http://localhost:9090 PORT=7860
EXPOSE 7860
CMD ["bash", "start.sh"]
