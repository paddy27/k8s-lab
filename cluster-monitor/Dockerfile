# Stage 1: build the React frontend
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: the FastAPI app, serving the built frontend as static files
FROM python:3.11-slim
WORKDIR /app

COPY backend/requirements.txt .
# See the cryptography== pin in requirements.txt for why this isn't a
# plain, unconstrained install.
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend-build /frontend/dist ./app/static

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
