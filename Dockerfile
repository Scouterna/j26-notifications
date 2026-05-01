# Use a slim Python image for the application
FROM python:3.14-slim

# Install uv as the package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set timezone to local for log improvements
ENV TZ="Europe/Stockholm"

# Set environment variables to prevent Python from writing .pyc files on build
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Install Python dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Copy the backend source code into the container
COPY src/ ./src
ENV PYTHONPATH=/app/src

# Expose the port the app runs on
EXPOSE 8000

# Use a Python script to start the apps
CMD ["python", "src/start.py"]
