# Пульт: выкладка новой версии и откат (E10-02, ADR-081).
# shellcheck disable=SC2016  # $POSTGRES_USER и т. п. раскрывает оболочка контейнера
#
# Обновить: снимок базы → код из git → сборка → миграции (sqlmigrate — в журнал)
# → перезапуск → проверка, что сайт открывается. Не открылся — сам возвращает
# прошлую версию. Откатить: миграции, которых не было в прошлой версии, — назад
# (если обратимы), код и образ — прошлые. Каждая выкладка — строка в
# deploy/logs/history: когда, с какой версии, на какую.

HISTORY="$ROOT/deploy/logs/history"

history_add() {
    mkdir -p "$LOG_DIR"
    printf '%s %s %s %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" "$2" "$3" >>"$HISTORY"
}

# Снимок базы перед выкладкой — в backups/, последние 5. Архив B2 и так помнит
# каждую минуту; этот — для быстрого возврата прямо на сервере.
pre_dump() {
    local name=$1 file
    mkdir -p "$BACKUP_DIR"
    file="$BACKUP_DIR/$(date +%Y-%m-%d-%H%M)-$name.dump"
    say "Снимок базы перед изменением: $file"
    dc exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"$file"
    [ -s "$file" ] || fail "снимок базы пустой"
    done_msg "снимок: $(du -h "$file" | cut -f1)"
    # Старые снимки перед выкладками — кроме пяти последних.
    find "$BACKUP_DIR" -maxdepth 1 -name '*-before-*.dump' -printf '%T@ %p\n' | sort -rn \
        | tail -n +6 | cut -d' ' -f2- | xargs -r rm -f
}

# Миграции, которые применит выкладка, — списком и SQL в журнал.
log_migrations() {
    local version=$1 plan
    plan=$(ver "$version" dc run --rm --no-deps -T app \
        python manage.py migrate --plan 2>/dev/null | grep -E '^[a-z_]+\.[0-9]{4}_' || true)
    if [ -z "$plan" ]; then
        echo "  новых миграций нет"
        return
    fi
    printf '%s\n' "$plan"
    local line app name
    while read -r line; do
        app=${line%%.*}
        name=${line#*.}
        name=${name%% *}
        echo "--- sqlmigrate $app $name"
        ver "$version" dc run --rm --no-deps -T app python manage.py sqlmigrate "$app" "$name" \
            2>&1 || true
    done <<<"$(echo "$plan" | awk '{print $1}')"
}

op_update() {
    cd "$ROOT" || return 1
    git fetch --quiet origin "$BRANCH" || {
        fail "не получилось забрать код из git"
        pause
        return 1
    }
    local current target changes
    current=$(git rev-parse HEAD)
    target=$(git rev-parse "origin/$BRANCH")
    if [ "$current" = "$target" ]; then
        ui_show "Обновление" "Уже последняя версия: $(short_sha) — $(git log -1 --format=%s)."
        return 0
    fi
    if ! git merge-base --is-ancestor "$current" "$target"; then
        ui_show "Обновление" "Версия на сервере не предок $BRANCH — обновление без перемотки опасно, разберитесь вручную."
        return 1
    fi
    changes=$(git log --format='• %s' "$current..$target" | head -20)
    ui_confirm "Обновить $(short_sha "$current") → $(short_sha "$target")?"$'\n\n'"$changes"$'\n\n'"Перед обновлением — снимок базы. Сайт будет недоступен около минуты." \
        || return 0
    run_logged update do_update "$current" "$target"
}

do_update() {
    local current=$1 target=$2 started prev_image version
    started=$(date +%s)
    version=$(short_sha "$target")
    prev_image=$(running_image)
    say "Обновление $(short_sha "$current") → $version"
    pre_dump "before-$version"

    say "Код"
    git -C "$ROOT" merge --ff-only --quiet "$target"
    done_msg "$(git -C "$ROOT" log -1 --format='%h %s')"

    say "Сборка образов"
    ver "$version" dc build app || {
        git -C "$ROOT" reset --hard --quiet "$current"
        fail "сборка не удалась — код возвращён, работает прежняя версия"
        return 1
    }
    dc build postgres

    say "Миграции"
    local plan
    plan=$(log_migrations "$version")
    printf '%s\n' "$plan"
    ver "$version" dc run --rm -T app python manage.py migrate --noinput || {
        git -C "$ROOT" reset --hard --quiet "$current"
        fail "миграции не применились — код возвращён, работает прежняя версия"
        return 1
    }

    say "Перезапуск"
    docker tag "$APP_IMAGE:$version" "$APP_IMAGE:latest"
    ver "$version" dc up -d --remove-orphans

    say "Проверка: сайт открывается"
    if wait_healthy; then
        history_add update "$(short_sha "$current")" "$version"
        done_msg "версия $version работает · $(($(date +%s) - started)) с"
        return 0
    fi
    warn "сайт не открылся — возвращаю прежнюю версию"
    git -C "$ROOT" reset --hard --quiet "$current"
    if [ -n "$prev_image" ]; then
        docker tag "$prev_image" "$APP_IMAGE:latest"
        ver "${prev_image##*:}" dc up -d
    fi
    history_add update-failed "$(short_sha "$current")" "$version"
    if wait_healthy; then
        if grep -q '^--- sqlmigrate' <<<"$plan"; then
            fail "версия $version не поднялась, работает прежняя ${prev_image##*:}. Её миграции остались в базе — обычно прежней версии они не мешают; список — выше в журнале"
        else
            fail "версия $version не поднялась, работает прежняя ${prev_image##*:}"
        fi
    else
        fail "и прежняя версия не открывается — смотрите «Журналы служб»"
    fi
    return 1
}

# Миграции, которых нет в версии $1: «приложение имя_цели» на каждое приложение
# (цель — последняя миграция той версии или zero).
rollback_targets() {
    local prev=$1 file app last
    git -C "$ROOT" diff --name-only --diff-filter=A "$prev" HEAD -- 'apps/*/migrations/0*.py' \
        | while read -r file; do
            echo "${file#apps/}" | cut -d/ -f1
        done | sort -u | while read -r app; do
            last=$(git -C "$ROOT" ls-tree --name-only "$prev" "apps/$app/migrations/" \
                | sed -n 's|.*/\(0[0-9]*_[^/]*\)\.py$|\1|p' | sort | tail -n 1)
            echo "$app ${last:-zero}"
        done
}

op_rollback() {
    cd "$ROOT" || return 1
    local last prev targets
    last=$(grep -E ' update ' "$HISTORY" 2>/dev/null | tail -n 1 || true)
    if [ -z "$last" ]; then
        ui_show "Откат" "Выкладок через пульт ещё не было — откатывать не на что."
        return 0
    fi
    prev=$(awk '{print $4}' <<<"$last")
    if [ "$(short_sha)" != "$(awk '{print $5}' <<<"$last")" ]; then
        ui_show "Откат" "Сейчас на сервере $(short_sha), а последняя выкладка — на $(awk '{print $5}' <<<"$last"). Откатывать вслепую не буду."
        return 1
    fi
    targets=$(rollback_targets "$prev")
    ui_confirm "Откатить $(short_sha) → $prev?"$'\n\n'"Отменится:"$'\n'"$(git log --format='• %s' "$prev..HEAD" | head -15)"$'\n\n'"Миграции назад: ${targets:-нет}"$'\n'"Перед откатом — снимок базы." \
        || return 0
    run_logged rollback do_rollback "$prev" "$targets"
}

do_rollback() {
    local prev=$1 targets=$2 current app target
    current=$(short_sha)
    say "Откат $current → $prev"
    pre_dump "before-rollback-$current"
    if [ -n "$targets" ]; then
        say "Миграции назад — кодом текущей версии, он знает, как их отменить"
        while read -r app target; do
            ver "$current" dc run --rm -T app python manage.py migrate "$app" "$target" || {
                fail "миграцию $app не отменить (необратимая?) — откат остановлен, работает $current"
                return 1
            }
        done <<<"$targets"
    fi
    say "Код и образ $prev"
    git -C "$ROOT" reset --hard --quiet "$prev"
    if ! docker image inspect "$APP_IMAGE:$prev" >/dev/null 2>&1; then
        ver "$prev" dc build app
    fi
    docker tag "$APP_IMAGE:$prev" "$APP_IMAGE:latest"
    ver "$prev" dc up -d --remove-orphans
    say "Проверка: сайт открывается"
    if wait_healthy; then
        history_add rollback "$current" "$prev"
        done_msg "работает $prev"
        return 0
    fi
    fail "после отката сайт не открывается — смотрите «Журналы служб»"
}

op_history() {
    local text
    text=$(tac "$HISTORY" 2>/dev/null | head -30 | awk '{printf "%s %s  %-15s %s → %s\n", $1, $2, $3, $4, $5}')
    ui_show "История выкладок" "${text:-Выкладок через пульт ещё не было.}"
}
