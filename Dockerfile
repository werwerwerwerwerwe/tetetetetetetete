FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Установка curl и сертификатов
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates

# Установка uv
ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

# Установка PYTHONPATH для импорта app.*
ENV PYTHONPATH=/app

# Копирование проекта
WORKDIR /app
COPY . .

# Установка зависимостей
RUN uv sync --frozen

CMD ["uv", "run", "main.py"]
