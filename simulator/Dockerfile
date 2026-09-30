FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY metricsim ./metricsim
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080 9101-9130 9201-9220
ENTRYPOINT ["metricsim"]
CMD ["run"]
