# 18. Свой сервер: первая настройка, перенос и выкладка руками

Гайд для администратора: как с нуля поднять систему на своём сервере, перенести
туда рабочую базу и дальше выкладывать новые версии самому — командами, без
помощника. У каждой команды — что она делает. Решения — ADR-081; что делать при
авариях, бэкапах и оповещениях — `docs/17-OPERATIONS.md`.

Сервер на 14.11.2026: Hetzner, AlmaLinux 10.2, 4 ядра, 8 ГБ, 151 ГБ, Docker уже
стоит, свой постоянный IP, домена нет. Ниже `<IP>` — адрес сервера, `<путь>` —
секретный путь системы (`SITE_PATH`), `<логин>` — ваш логин на сервере.

**Где выполнять.** «На сервере» — в SSH-сессии на сервере. «Локально» — в
терминале WSL в папке проекта (`/mnt/r/web/www/seo-system`), где работает 8000.

**Время.** Подготовка сервера — около часа, перенос базы — полчаса, выкладка
новой версии потом — 2–5 минут.

---

## 0. Что нужно до начала

- Вход на сервер по SSH-ключу и права администратора (`sudo`) — или вход под `root`.
- Открытые снаружи порты 22, 80 и 443 (если у провайдера свой файрвол — у админа).
- Секретный путь системы — тот же, что в локальном `.env`.
- Последняя версия кода — на GitHub: локально `git push` ветки `main` (её пушите вы).
- Под рукой — локальный `.env`: из него берутся ключи сервисов (Serper,
  Collaborator, GlitchTip и др.).

---

## 1. Подготовка сервера (один раз)

### 1.1. Посмотреть, что за сервер

```bash
cat /etc/os-release | head -3     # какая система: ждём AlmaLinux 10
uname -m                          # процессор: x86_64
free -h; df -h /                  # память и диск
docker --version                  # Docker уже стоит
docker compose version            # и его модуль compose — им запускается система
```

Если `docker compose version` пишет «unknown command» — `sudo dnf -y install
docker-compose-plugin`.

### 1.2. Обновить систему и поставить нужные программы

```bash
sudo dnf -y upgrade                         # все обновления системы (как apt upgrade)
sudo dnf -y install git newt tar            # git — код; newt — окна меню пульта (whiptail)
sudo systemctl enable --now docker          # Docker стартует сам после перезагрузки сервера
sudo reboot                                 # если обновилось ядро — перезагрузка, через минуту войти снова
```

`dnf` — менеджер пакетов AlmaLinux (как `apt` в Ubuntu). `systemctl enable --now`
— «запусти сейчас и при каждой загрузке».

### 1.3. Свой пользователь, если входите под root

Работать под `root` можно, но ошибка в команде тогда бьёт по всему серверу.
Если входите под `root` — заведите пользователя (пусть `seo`):

```bash
adduser seo                                   # новый пользователь
usermod -aG wheel,docker seo                  # wheel — право на sudo; docker — запускать контейнеры
mkdir -p /home/seo/.ssh
cp ~/.ssh/authorized_keys /home/seo/.ssh/     # тот же ключ входа, что у root
chown -R seo:seo /home/seo/.ssh && chmod 700 /home/seo/.ssh
passwd seo                                    # пароль — только для sudo, входить по нему нельзя
```

Группа `docker` — почти права root: кто в ней, тот управляет всеми контейнерами.
Дальше входите как `ssh seo@<IP>`. Если уже входите не под root — пропустите шаг
и добавьте себя в группу: `sudo usermod -aG docker $USER`, выйти и войти снова.

### 1.4. SSH: только по ключу

```bash
sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin|port) '
```

`sshd -T` печатает действующие настройки SSH. Нужно: `passwordauthentication no`,
`permitrootlogin no` (или `prohibit-password`). Если не так:

```bash
sudo tee /etc/ssh/sshd_config.d/01-seo.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin no
EOF
sudo sshd -t && sudo systemctl reload sshd     # проверить настройки, потом применить
sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin) '   # теперь no / no
```

Файл — `01-…`, а не `50-…`: SSH берёт **первое** встреченное значение, а на
облачных образах рядом лежит `50-cloud-init.conf`, который может разрешать вход
по паролю.

**Не закрывайте текущее окно**, пока во втором окне не войдёте заново по ключу:
ошибка в настройках SSH — потерянный доступ к серверу. `PermitRootLogin no` —
только если уже есть свой пользователь (шаг 1.3).

### 1.5. Файрвол: наружу только 22, 80, 443

```bash
sudo dnf -y install firewalld                  # если не стоит
sudo systemctl enable --now firewalld          # запустить и включать при загрузке
sudo firewall-cmd --permanent --add-service=ssh --add-service=http --add-service=https
sudo firewall-cmd --permanent --remove-service=cockpit   # веб-панель AlmaLinux — не нужна
sudo firewall-cmd --reload                     # применить
sudo firewall-cmd --list-all                   # services: http https ssh
```

`--permanent` — правило переживёт перезагрузку; без `--reload` оно ещё не
действует. Если SSH у вас не на 22 — сначала `--add-port=<порт>/tcp`, иначе
закроете себе вход.

Важно: Docker открывает порты своих контейнеров в обход firewalld. Поэтому в
`compose.prod.yaml` наружу смотрит только Caddy (80 и 443); база и Redis портов
не публикуют совсем.

### 1.6. Обновления безопасности — сами, каждый день

```bash
sudo dnf -y install dnf-automatic
sudo sed -i 's/^upgrade_type.*/upgrade_type = security/; s/^apply_updates.*/apply_updates = yes/' \
    /etc/dnf/automatic.conf                     # ставить только исправления безопасности
sudo systemctl enable --now dnf-automatic.timer
grep -E '^(upgrade_type|apply_updates)' /etc/dnf/automatic.conf   # security / yes
```

Если файла `/etc/dnf/automatic.conf` нет — у вас новая версия dnf, пришлите
вывод `ls /etc/dnf/`: настройка там в другом месте.

### 1.7. Точное время

```bash
timedatectl          # «System clock synchronized: yes», «NTP service: active»
```

Время важно для кодов Google Authenticator: если часы сервера уйдут, коды
перестанут подходить. Если `no` — `sudo systemctl enable --now chronyd`.

### 1.8. Подкачка 2 ГБ — запас памяти на редкий пик

```bash
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048   # файл на 2 ГБ
sudo chmod 600 /swapfile                             # читать его может только система
sudo mkswap /swapfile && sudo swapon /swapfile       # сделать подкачкой и включить
echo '/swapfile none swap defaults 0 0' | sudo tee -a /etc/fstab   # и после перезагрузки
free -h                                              # Swap: 2.0Gi
```

### 1.9. SELinux — не выключать

```bash
getenforce           # Enforcing
```

SELinux — встроенная защита AlmaLinux: процесс видит только свои файлы. Docker с
ней дружит; единственный файл с диска, который читает контейнер, — настройки
Caddy, у него в `compose.prod.yaml` метка `z`. Выключать SELinux не нужно.

---

## 2. Код на сервер

### 2.1. Ключ «только чтение» к репозиторию GitHub

Сервер скачивает код сам, из GitHub. Для этого у него свой ключ, который умеет
только читать один репозиторий:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github-seo -N "" -C "seo-server deploy"   # ключ без пароля
cat ~/.ssh/github-seo.pub                                                  # открытая часть
```

На GitHub: репозиторий → **Settings → Deploy keys → Add deploy key**. Название —
`seo-server`, ключ — строка из `cat`, **Allow write access — не отмечать**.

Сказать SSH, каким ключом ходить на GitHub, и проверить:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/github-seo
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com      # первый раз спросит про отпечаток GitHub — ответить yes;
                           # дальше «Hi EvolDev/seo-system! You've successfully authenticated…»
```

Если подключение висит — исходящий порт 22 закрыт; тогда в блок `Host github.com`
добавить `Hostname ssh.github.com` и `Port 443`.

### 2.2. Скачать код

```bash
sudo mkdir -p /opt/seo-system && sudo chown $USER: /opt/seo-system   # папка системы — ваша
git clone git@github.com:EvolDev/seo-system.git /opt/seo-system
cd /opt/seo-system
touch .seo-server                                                     # метка для пульта: «это сервер»
mkdir -p backups
```

### 2.3. Короткая команда для управления

Все команды ниже — `docker compose` с именем проекта и файлом прода. Чтобы не
писать это каждый раз:

```bash
echo "alias dcp='docker compose -p seo -f /opt/seo-system/compose.prod.yaml'" >> ~/.bashrc
source ~/.bashrc
```

`dcp ps` — то же, что `docker compose -p seo -f /opt/seo-system/compose.prod.yaml ps`.

---

## 3. Настройки сервера — файл `.env`

`.env` — все настройки и секреты сервера: пароль базы, ключи сервисов, адрес. В
git он не попадает (`.gitignore`), живёт только на сервере, **копия — в вашем
менеджере паролей**: без неё на новом сервере систему не поднять.

```bash
cd /opt/seo-system
cp .env.example .env && chmod 600 .env        # из образца; читать может только владелец
nano .env                                     # или vi
```

Новые секреты — сгенерировать здесь же:

```bash
openssl rand -base64 48 | tr -d '\n'; echo    # для DJANGO_SECRET_KEY
openssl rand -hex 24                          # для POSTGRES_PASSWORD
```

Что заполнить (остальное — как в образце):

| Строка | Значение | Зачем |
|---|---|---|
| `DJANGO_ENV` | `prod` | прод-настройки: без отладки, только HTTPS, вход с кодом |
| `DJANGO_SECRET_KEY` | сгенерированный | подписывает сессии; свой у сервера |
| `DJANGO_DEBUG` | `false` | (в проде отладка выключена в любом случае) |
| `ALLOWED_HOSTS` | `<IP>` | система отвечает только на свой адрес |
| `TIME_ZONE` | как локально | |
| `POSTGRES_HOST` | `postgres` | база — соседний контейнер |
| `POSTGRES_DB`, `POSTGRES_USER` | `seo`, `seo` | имя базы и её владельца |
| `POSTGRES_PASSWORD` | сгенерированный | пароль базы — новый, не локальный |
| `REDIS_URL` | `redis://redis:6379/0` | очередь задач |
| `SITE_ADDRESS` | `<IP>` | адрес для Caddy и сертификата |
| `SITE_PATH` | `<путь>` | секретный путь системы (как локально) |
| `TLS_MODE` | `ip-staging` | сначала пробный сертификат, потом `ip` (§6.2) |
| `WEB_WORKERS` | `3` | процессов сайта — для 8 ГБ хватает |
| `BACKUP_MONITOR`, `PG_ARCHIVE_MODE` | `false`, `off` | бэкапы включаем, когда будет B2 (§11) |
| `SENTRY_DSN`, `SERPER_API_KEY`, `COLLABORATOR_API_KEY` и другие ключи | из локального `.env` | те же сервисы, те же ключи |

`LOGIN_OTP_REQUIRED` не писать: в проде вход с кодом включён сам.

---

## 4. Сборка

```bash
cd /opt/seo-system
dcp build             # образ приложения (код + статика) и образ базы (Postgres + инструменты бэкапа)
dcp up -d postgres redis   # пока только база и очередь: базу сейчас наполним
dcp ps                # postgres — healthy, redis — healthy
```

`build` — первый раз 5–10 минут. `up -d` — запустить в фоне (`-d` — «отвязаться
от терминала»). Пустая база заводится сама при первом запуске — с именем,
владельцем и паролем из `.env`.

---

## 5. Перенос базы и файлов с локальной версии

**Перед переносом — договоритесь со всеми:** после него работа идёт только на
сервере. Что сделано в локальной версии после снимка, на сервер не попадёт.

### 5.1. Локально: снимок базы, файлы загрузок, числа строк

```bash
cd /mnt/r/web/www/seo-system
docker compose exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > backups/transfer.dump                                   # вся база одним файлом
tar -czf backups/uploads.tgz -C uploads .                     # файлы загрузок (прайсы, каталоги)
docker compose exec -T postgres sh -c 'psql -XAtq -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < deploy/ops/table-counts.sql > backups/counts-local.txt  # сколько строк в каждой таблице
ls -lh backups/transfer.dump backups/uploads.tgz              # ~37 МБ и ~60 МБ
```

`pg_dump -Fc` — снимок базы в сжатом формате Postgres; локальная база при этом
работает, её только читают.

### 5.2. Скопировать на сервер

```bash
scp backups/transfer.dump backups/uploads.tgz backups/counts-local.txt \
    <логин>@<IP>:/opt/seo-system/backups/
```

`scp` — копирование по SSH тем же ключом, которым вы входите. Если ключ не в
`~/.ssh` WSL — добавьте `-i <путь к ключу>`; можно и WinSCP.

### 5.3. На сервере: развернуть базу

```bash
cd /opt/seo-system
dcp exec -T postgres sh -c 'pg_restore --no-owner --no-acl --exit-on-error \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backups/transfer.dump     # снимок → пустая база сервера
dcp run --rm app python manage.py migrate   # миграции, которых нет в снимке (обычно ни одной)
```

`--no-owner --no-acl` — владельцем всего станет пользователь базы с сервера, а не
локальный; `--exit-on-error` — при первой же ошибке остановиться, а не оставить
базу наполовину. Если остановился — пришлите ошибку; начать заново с пустой базы:
`dcp exec postgres sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'`.
`run --rm app …` — разовый контейнер приложения для команды, после — удаляется.

### 5.4. На сервере: файлы загрузок

```bash
dcp run --rm --no-deps -v /opt/seo-system/backups:/transfer:ro app \
    sh -c 'tar -xzf /transfer/uploads.tgz -C /app/uploads && find /app/uploads -type f | wc -l'
```

Распаковка — внутри контейнера приложения, его пользователем: файлы сразу с
правильным владельцем, в общем томе, который видят сайт и воркер.

### 5.5. Сверка: всё ли приехало

```bash
dcp exec -T postgres sh -c 'psql -XAtq -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < deploy/ops/table-counts.sql > backups/counts-server.txt
diff backups/counts-local.txt backups/counts-server.txt && echo "все таблицы совпали"
```

Пусто от `diff` и «все таблицы совпали» — перенос полный.

---

## 6. Запуск и сертификат HTTPS

### 6.1. Запустить систему

```bash
dcp up -d          # всё: сайт, фоновые задачи, расписание, веб-сервер, бэкапы
dcp ps             # app — healthy, остальные — running
```

Контейнер бэкапов (`backup`) без Backblaze B2 просто ждёт: в его журнале
«бэкапы выключены» — так и надо, включим в §11.

### 6.2. Сертификат на IP — сначала пробный

```bash
dcp logs caddy | grep -iE 'certificate obtained|error'
```

С `TLS_MODE=ip-staging` Caddy берёт сертификат у тестового центра Let's Encrypt:
браузер ему не доверяет, зато так проверяется, что выдача на IP проходит, а лимиты
настоящего центра не тратятся. Ждём строку `certificate obtained successfully`.

Получилось — в `.env` поставить `TLS_MODE=ip` и пересоздать Caddy:

```bash
dcp up -d caddy                 # видит, что настройка поменялась, и пересоздаёт
dcp logs -f caddy               # ждём «certificate obtained successfully», выход — Ctrl+C
```

Сертификат на IP живёт 160 часов, Caddy продлевает его сам раз в пару дней.

Не получилось (в журнале `rejectedIdentifier` или похожая ошибка) — пришлите
строки с ошибкой: тогда HTTPS только через домен.

### 6.3. Проверка снаружи

С любого компьютера, лучше без VPN:

- `https://<IP>/` — соединение обрывается, страницы нет — так и задумано;
- `https://<IP>/<путь>/` — страница входа с замком в адресной строке.

С сервера:

```bash
curl -s https://<IP>/<путь>/healthz; echo     # «ok» — сайт и база живы
```

### 6.4. Первый вход и люди

1. Войти своим логином и паролем (как локально) → привязать телефон (QR-код) →
   сохранить запасные коды.
2. **Настройки → Пользователи**: удалить `test1` и пустого `Evgeny`; у Evgeniy и
   Artem — «Может входить», роль «Линкбилдер», новый пароль.
3. Пароли передать лично. При первом входе каждый привяжет свой телефон
   (справка «Как входить с кодом из телефона»).

### 6.5. Локальная версия после переезда

Она становится копией для разработки: работать в ней перестаём. Платные задачи по
расписанию (индексация и т. п.) — только на сервере, иначе кредиты Serper
тратятся дважды.

---

## 7. Выложить новую версию — руками

Порядок всегда один. Сначала локально — `git push` ветки `main` на GitHub.

```bash
cd /opt/seo-system

# 1. Снимок базы — чтобы было куда вернуться
dcp exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > backups/$(date +%F-%H%M)-before-update.dump

# 2. Что приедет
git fetch                                   # узнать, что нового на GitHub (код ещё не меняется)
git log --oneline HEAD..origin/main         # список новых коммитов

# 3. Код
git pull --ff-only                          # только перемотка вперёд; если код на сервере правили руками — откажет

# 4. Образ приложения с новым кодом
dcp build app                               # 1–3 минуты; сайт пока работает на старом образе

# 5. Миграции базы
dcp run --rm app python manage.py migrate --plan   # что изменится в базе
dcp run --rm app python manage.py migrate          # применить

# 6. Перезапуск на новом образе
dcp up -d                                   # пересоздаёт контейнеры, у которых поменялся образ

# 7. Проверка
dcp ps                                      # app — healthy
curl -s https://<IP>/<путь>/healthz; echo   # ok
```

Сайт недоступен только на шаге 6 — секунд 10–20.

**Особые случаи:**

| Что поменялось в коммитах | Дополнительно |
|---|---|
| `deploy/Caddyfile` | `dcp exec caddy caddy reload --config /etc/caddy/Caddyfile` — Caddy перечитает настройки без остановки |
| `deploy/postgres/` (образ базы) | `dcp build postgres && dcp up -d postgres backup` — база перезапустится, ~30 с недоступности |
| `.env` (правили сами) | `dcp up -d` — пересоздаст контейнеры с новыми настройками |
| `compose.prod.yaml` | `dcp up -d` |

Старые снимки перед выкладками копятся в `backups/` — раз в месяц удалить
ненужные: `ls -lh backups/`.

---

## 8. Откат — если новая версия сломалась

**Быстро и без потери работы — через пульт:** `deploy/seo` → «Откатить на прошлую
версию». Он сам вернёт миграции, код и прошлый образ (`docs/17-OPERATIONS.md` §2).

**Руками:**

```bash
cd /opt/seo-system
git log --oneline -5                    # прошлая версия — вторая строка
git reset --hard <прошлый коммит>       # код — назад
dcp build app && dcp up -d              # образ прошлой версии и перезапуск
```

Если новая версия успела изменить базу (были миграции) — вернуть и базу снимком
из шага 7.1. **Всё, что люди сделали после снимка, пропадёт**:

```bash
dcp stop app worker beat                # чтобы в базу никто не писал
dcp exec -T postgres sh -c 'pg_restore --clean --if-exists --no-owner --no-acl \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backups/<снимок>.dump
dcp up -d
```

Достать одну-две записи без отката всей базы — «копия базы на минуту»
(`docs/17-OPERATIONS.md` §4), когда включены бэкапы.

---

## 9. Пульт

```bash
/opt/seo-system/deploy/seo
```

Меню стрелками: состояние, обновить, откатить, службы, журналы, бэкапы,
восстановление, ручные команды. «Обновить» делает ровно §7, но ещё сам
возвращает прежнюю версию, если новая не открылась, и пишет историю выкладок.
Руками или пультом — на ваш выбор; одно другому не мешает.

---

## 10. Повседневные команды

```bash
dcp ps                                  # что запущено и здорово ли
dcp logs -f --tail 100 app              # журнал сайта вживую (worker, beat, caddy — так же)
dcp restart app                         # перезапустить сайт
dcp exec app python manage.py shell     # консоль Django
dcp exec app python manage.py queue_check   # жива ли очередь задач
df -h /; free -h                        # диск и память
docker image prune -f                   # удалить образы, которые больше не нужны
```

`dcp down` останавливает всё, но данные остаются в томах (`seo_pgdata`,
`seo_uploads`). **Никогда не `dcp down -v`** — `-v` удаляет тома вместе с базой.

---

## 11. Бэкапы — когда будет Backblaze B2

1. Корзина в B2 — `docs/17-OPERATIONS.md` §3 «Хранилища — один раз».
2. В `.env`: `B2_ENDPOINT`, `B2_REGION`, `B2_BUCKET`, `B2_KEY_ID`, `B2_APPLICATION_KEY`,
   `BACKUP_WALG_KEY` (`openssl rand -hex 32`), `BACKUP_FILES_PASSWORD`
   (`openssl rand -base64 32`), `PG_ARCHIVE_MODE=on`, `BACKUP_MONITOR=true`.
   **Оба ключа шифрования — сразу в менеджер паролей**: без них копии не прочитать.
3. Применить:

```bash
dcp up -d postgres backup      # база перезапустится с архивом изменений, бэкапы проснутся
dcp logs -f backup             # первый снимок, ночная копия и проверка — сразу
```

Через несколько минут в **Служебное → Бэкапы** — всё «✓ Готово».

---

## 12. Домен — когда появится

1. У администратора домена — запись `A`: `<имя>.<домен>` → `<IP>`.
2. В `.env`: `SITE_ADDRESS=<имя>.<домен>`, `ALLOWED_HOSTS=<имя>.<домен>`, `TLS_MODE=auto`.
3. `dcp up -d` — Caddy получит обычный сертификат на домен.
4. Людям — новый адрес `https://<имя>.<домен>/<путь>/`, войти заново.
