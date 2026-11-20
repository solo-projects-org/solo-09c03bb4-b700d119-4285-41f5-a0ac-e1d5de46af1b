FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# 月度 CSV 通过 compose 挂载到 /data/<站点>/YYYY-MM.csv
ENV STATION_DATA_DIR=/data \
    PORT=8000

EXPOSE 8000

# 用 flask run 启动（容器内单进程场景足够；STATION_NAME 由 compose 注入）
CMD ["flask", "--app", "app", "run", "--host", "0.0.0.0", "--port", "8000"]
