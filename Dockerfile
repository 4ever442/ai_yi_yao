# Dockerfile（备用：Render 也支持 Docker 部署）
# 项目是纯标准库 Python，用官方 slim 镜像即可。
FROM python:3.12-slim

WORKDIR /app

# 无需 pip install（零第三方依赖）
COPY core/ ./core/
COPY web/ ./web/
COPY data/ ./data/
COPY server.py ./

ENV MEDHUB_HOST=0.0.0.0
ENV PORT=8100
ENV MEDKG_VEC=0

EXPOSE 8100

CMD ["python", "server.py", "--no-browser"]
