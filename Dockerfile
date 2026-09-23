FROM python:3.11-slim

WORKDIR /code

# Install dependencies first so they cache across source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY llm ./llm
COPY graph ./graph
COPY rag ./rag
COPY eval ./eval

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 recoup
USER recoup

EXPOSE 8000

# No --reload in the image; docker-compose enables it for local development.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
