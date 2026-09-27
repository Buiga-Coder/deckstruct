# Развёртывание

Новый проект: `/opt/deckstruct`, Compose project `deckstruct`, фронт на порту `8081`. ВК-проект в `/opt/ipmkn-start` не изменяется. Новый фронт ограничен 64 МБ RAM, 0,25 CPU и 64 процессами; файловая система и bind mounts доступны только для чтения. Логи ротируются.

Каждая версия хранится в отдельном каталоге `releases/<version>`, `current` указывает на активную. Перед деплоем проверить свободный порт, память и состояние существующих контейнеров. Не запускать глобальные `docker prune`, `docker compose down --volumes` или перезапуск Docker.

## Выпуск из локального Git

В корне локального проекта (после коммита):

```sh
git archive --format=tar.gz --output=deckstruct.tar.gz HEAD
scp deckstruct.tar.gz root@94.228.166.245:/opt/deckstruct/incoming.tar.gz
```

На сервере выбрать новое уникальное имя версии, затем:

```sh
release=/opt/deckstruct/releases/REPLACE_WITH_UNIQUE_VERSION
mkdir "$release"
tar -xzf /opt/deckstruct/incoming.tar.gz -C "$release"
cd "$release"
docker compose -p deckstruct config --quiet
docker compose -p deckstruct up -d --wait --wait-timeout 60
curl -fsS http://127.0.0.1:8081/healthz
ln -sfn "$release" /opt/deckstruct/current
docker compose -p deckstruct ps
```

Не обновлять `current`, если проверки не прошли. При неудаче восстановить предыдущую сборку командами ниже. Пересоздание единственного контейнера допускает краткую паузу доступности.

## Откат

Выбрать существующий предыдущий release; затем из его каталога выполнить `docker compose -p deckstruct up -d --wait`, проверить `/healthz`, обновить ссылку `current`. Предыдущие версии и образ не удалять до проверки новой сборки.

## Диагностика

```sh
cd /opt/deckstruct/current
docker compose -p deckstruct ps
docker compose -p deckstruct logs --tail 100 frontend
docker stats --no-stream
free -m
```

HTTP на 8081 предназначен для демо. Перед реальными логинами и файлами подключить отдельный домен и HTTPS. Конфигурацию действующего reverse proxy ВК-бота сейчас не меняем.

Принцип изоляции Compose: https://docs.docker.com/compose/how-tos/project-name/
