# Project bot: chỉ phần Telegram, không database / logic nghiệp vụ – mọi thứ gọi web API (API_URL + SERVICE_TOKEN).
# Build: cd bot && docker compose build   (context = thư mục bot/)
# cố định bản Python + digest (build lại ra đúng image, không tự đổi bản vá); nâng cấp: đổi cả tag lẫn digest
FROM python:3.12.15-slim-trixie@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Ho_Chi_Minh

WORKDIR /app

COPY Pipfile Pipfile.lock ./
RUN pip install pipenv \
    && pipenv install --system --deploy \
    && pip uninstall -y pipenv virtualenv

COPY *.py ./

USER 1000:1000

CMD ["python", "bot.py"]
