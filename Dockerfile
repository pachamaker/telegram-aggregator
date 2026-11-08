FROM python:3.12-slim
WORKDIR /app
COPY --chmod=644 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=644 aggregator/ aggregator/
RUN chmod 755 aggregator
ENTRYPOINT ["python", "-B", "-u", "-m", "aggregator", "--config", "/config/config.json", "--state-dir", "/state"]
CMD ["run"]
