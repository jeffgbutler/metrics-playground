FROM python:3.12-slim
RUN pip install --no-cache-dir "markdown>=3.6"
WORKDIR /app
COPY server.py template.html ./
ENV PYTHONUNBUFFERED=1 SITE_ROOT=/site PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
