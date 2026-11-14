# База прода и бэкапы (E10-03, ADR-081). Тот же Postgres 16 плюс:
# - WAL-G — каждое изменение базы в течение минуты уходит в архив Backblaze B2,
#   ночью — полный снимок; из них база восстанавливается на любую минуту;
# - rclone — ночной дамп и файлы загрузок, зашифрованные, в B2 и Cloudflare R2;
# - supercronic — расписание бэкапов в контейнере backup;
# - jq — разбор ответов WAL-G и сверка таблиц при проверке восстановления.
# Образ общий у служб postgres и backup: архив пишет сама база, остальное — backup.
FROM postgres:16

ARG WALG_VERSION=v3.0.9
ARG WALG_SHA256=d6795c663894d836ba20840bffe8970aed81bbe25d3151bd4951f6f9c362def9
ARG RCLONE_VERSION=v1.75.2
ARG RCLONE_SHA256=efbfe852181f7191eb3c9043ed1ab49c9b2d0ba045c61c926a5da111c939ec5c
ARG SUPERCRONIC_VERSION=v0.2.51
ARG SUPERCRONIC_SHA256=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123

# Каждый файл сверяется с контрольной суммой: подменённый на сервере релиз
# не соберётся.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl jq \
    && curl -fsSL -o /usr/local/bin/wal-g \
        "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-amd64" \
    && echo "${WALG_SHA256}  /usr/local/bin/wal-g" | sha256sum -c - \
    && chmod +x /usr/local/bin/wal-g \
    && curl -fsSL -o /tmp/rclone.deb \
        "https://github.com/rclone/rclone/releases/download/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-amd64.deb" \
    && echo "${RCLONE_SHA256}  /tmp/rclone.deb" | sha256sum -c - \
    && dpkg -i /tmp/rclone.deb \
    && rm /tmp/rclone.deb \
    && curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Папки контейнера backup: рабочая (временные дампы, временные базы проверки)
# и эмулятора хранилища на тестовой копии. Пустые тома Docker при первом
# подключении получают владельца отсюда.
RUN mkdir -p /scratch /s3data /restore \
    && chown postgres:postgres /scratch /s3data /restore

# Право на запуск — здесь, а не из папки: в git скрипты могли записаться без него
# (папка проекта на диске Windows), и на сервере контейнер бэкапов не стартовал бы.
COPY backup/ /usr/local/lib/seo-backup/
RUN chmod 755 /usr/local/lib/seo-backup/*.sh
