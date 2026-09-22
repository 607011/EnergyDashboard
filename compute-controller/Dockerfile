FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller.py backtest.py tuya_client.py pc_actuator.py ./

CMD ["python", "-u", "controller.py"]
