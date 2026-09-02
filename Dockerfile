FROM python:3.13-slim-trixie

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY workout_mcp_server.py .
COPY static/ ./static/

# The SQLite database lives here -- this is the whole workout history, so it
# must be a mounted volume rather than image layers. See docker-compose.yml.
VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "workout_mcp_server.py"]
