FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Install curl to install uv
RUN apt-get update \
    && apt install -y ffmpeg \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer/manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv

# Create application directory
WORKDIR /app

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Create virtual environment outside bind mount and install deps
RUN uv sync --frozen --no-dev \
    && /opt/venv/bin/python -V

# Ensure our venv is first on PATH
ENV PATH="/opt/venv/bin:${PATH}"

# Copy the rest of the app
COPY . .

# Ensure entrypoint has Unix line endings and is executable
RUN sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh

# Ensure /tmp exists with safe permissions (sticky bit)
RUN mkdir -p /tmp && chmod 1777 /tmp

# Expose Django port
EXPOSE 8000

# Entrypoint runs migrations and dev server
CMD ["/bin/sh", "./docker/entrypoint.sh"]
