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

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
