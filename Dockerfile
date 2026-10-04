# One image for the whole app: build the React frontend, then package it with the FastAPI backend.

# ---- stage 1: build the frontend ----
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- stage 2: the app ----
FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ ./backend/
COPY --from=web /web/dist ./frontend/dist
WORKDIR /app/backend
# Bring the database up to date, then serve. Render supplies $PORT.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.server:site --host 0.0.0.0 --port ${PORT:-10000}"]
