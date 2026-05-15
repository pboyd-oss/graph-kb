FROM harbor.tuxgrid.com/docker.io/python:3.12-slim
WORKDIR /app

# CPU-only torch first — avoids pulling the 5GB CUDA build
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py analyzer.py ./
RUN mkdir -p /app/kb /app/documents

CMD ["python", "server.py"]
