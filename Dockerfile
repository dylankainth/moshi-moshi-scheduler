FROM python:3.13-slim
WORKDIR /app
COPY foresight ./foresight
RUN useradd -r -u 10001 foresight && mkdir /data && chown foresight /data
USER foresight
ENV FORESIGHT_HOME=/data PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["python", "-m", "foresight", "serve", "--host", "0.0.0.0", "--port", "8000"]
