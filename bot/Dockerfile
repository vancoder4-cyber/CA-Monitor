FROM python:3.12-slim

WORKDIR /app

# 中文字体(画月历需要),装完清缓存
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 长连接机器人,无需暴露端口
CMD ["python", "bot.py"]
