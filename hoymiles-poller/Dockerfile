FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hoymiles_client.py poller.py ./

CMD ["python", "-u", "poller.py"]
