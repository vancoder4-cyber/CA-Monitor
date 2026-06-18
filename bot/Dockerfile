FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# 安装依赖 + Chromium(含系统依赖,用于日历截图)
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# 长连接机器人,无需暴露端口
CMD ["python", "bot.py"]
