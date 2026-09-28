> **Ubuntu:** добавлен headless-рендер LibreOffice + Poppler. PowerPoint на Linux не нужен. Актуальная Linux-инструкция: [UBUNTU.md](UBUNTU.md). Описанные ниже требования PowerPoint относятся только к Windows-ветке.

# Локальное API парсера, версия 0.1

Это сервер для разработки на Windows, доступный только на 127.0.0.1. Для рендера
требуется установленный PowerPoint. Это не публичный многопользовательский сервис:
нет разделения пользователей, распределённой очереди и межпроцессной блокировки.
Не запускайте два сервера на одном storage. CLI продолжает работать отдельно.

## Запуск в PowerShell

```powershell
$env:PARSER_API_TOKEN=[guid]::NewGuid().ToString('N')
python -m template_parser.cli serve --config config.local.json --origin http://localhost:5173
```

Хранилище по умолчанию output/api_jobs, порт 8765. Их можно изменить через
--storage и --port. VLM-ключ берётся из окружения по config.local.json, как у CLI.
PARSER_API_TOKEN — другой секрет, только для локального API. Передайте его локальному
фронтенду вне Git. Для сохранения доступа после рестарта используйте тот же токен.
Фронту НИКОГДА не нужен VLM-ключ. Origin должен точно совпадать, включая порт;
http://localhost:5173 и http://127.0.0.1:5173 — разные origin. Без --origin разрешены
клиенты без заголовка Origin (например Python/PowerShell), но не cross-origin браузер.

Все запросы, включая картинки и ZIP, требуют Authorization: Bearer <PARSER_API_TOKEN>.
Для картинки используйте fetch → blob → URL.createObjectURL, затем revokeObjectURL;
обычный img src не умеет добавлять этот заголовок. Токены не помещать в URL.

## Маршруты

| Метод | Путь | Назначение |
|---|---|---|
| POST | /jobs | Raw PPTX в теле, ответ 202 с job_id и status_url |
| GET | /jobs/{job_id} | Статус, прогресс и последняя опубликованная version |
| POST | /jobs/{job_id}/resume | Продолжить partial/failed/interrupted, без тела |
| GET | /jobs/{job_id}/versions/{version}/result | JSON для UI, без исходного XML |
| GET | /jobs/{job_id}/versions/{version}/previews/sN.png | PNG выбранного слайда |
| GET | /jobs/{job_id}/versions/{version}/package | Полный ZIP для генератора |

Загрузка: Content-Type application/vnd.openxmlformats-officedocument.presentationml.presentation
или application/octet-stream. Отправлять File напрямую, НЕ FormData. Максимум 50 MiB,
распакованный ZIP до 500 MiB и 20000 записей. Сервер не принимает путь с компьютера
пользователя, имя файла, конфиг модели или переопределение ролей. Content-Length обязателен.
Очередь допускает до 4 задач, включая активную; заполнение возвращает локальный 429.

```javascript
const base = 'http://127.0.0.1:8765';
const headers = {Authorization: `Bearer ${localApiToken}`};
const response = await fetch(`${base}/jobs`, {
  method: 'POST', headers: {...headers, 'Content-Type': 'application/octet-stream'},
  body: fileInput.files[0],
});
if (!response.ok) throw new Error(`Upload failed: ${response.status}`);
const {job_id} = await response.json();
// Опрос примерно раз в 2–5 секунд, с остановкой при terminal status:
const stateResponse = await fetch(`${base}/jobs/${job_id}`, {headers});
const state = await stateResponse.json();
if (state.version) {
  const resultResponse = await fetch(
    `${base}/jobs/${job_id}/versions/${state.version}/result`, {headers});
  const result = await resultResponse.json();
  // Отобразить result.slides и result.validation.
}
```

Статусы: queued → extracting → rendering → analyzing → exporting → completed/partial.
failed означает исключение, interrupted — незавершённую задачу после рестарта сервера.
При partial доступны валидные результаты и отчёт об остальных слайдах. Неопределённости
needs_review/unassigned являются данными, а не обязательным шагом одобрения.
progress.counts относятся к анализу слайдов, не к проценту всего цикла.
Во время нового анализа предыдущая опубликованная version остаётся доступной.
На старте активные задачи помечаются interrupted, автоматически API не вызывается.
resume явно разрешает продолжить API-запросы; повторная отправка не создаёт второй запуск.
Ctrl+C прекращает HTTP-обслуживание, но ожидает текущего worker; завершение может быть
долгим из-за API. Принудительное закрытие процесса обнаруживается при следующем старте.

## Контракт результата

result содержит job_id, version, partial, slides, validation, package_url.
Слайд содержит slide_id, usage, needs_review, unassigned, components, preview_url.
components содержит исходные members/slots/preserve; результат не редактируется.
Полная геометрия, source_part/shape_id, object_catalog, assets и схемы остаются в ZIP.
Нет endpoint сохранения ролей. PUT/PATCH/DELETE возвращают 405. Неизвестные POST — 404.

Фронт передаёт генератору только job_id и version. Генератор должен самостоятельно
получить ZIP с доверенного адреса этого API и проверить контракт/checksums, а не
принимать присланные браузером роли за истину. version — ID неизменяемого снимка;
archive_sha256 в статусе относится к последнему опубликованному ZIP. Интеграция
на стороне отдельного генератора здесь не реализована.

401 — неверный токен/origin; 400 — плохой файл/состояние; 404 — неизвестный ресурс;
413 — размер; 415 — Content-Type; 429 — локальная очередь. Ошибки внешнего API
отражаются статусом partial и отчётом, не смешиваются с 429 загрузки.
Ответ failed содержит только класс исключения, без ключей, локальных путей и
сырых ответов провайдера. В production нужен отдельный сервер, авторизация пользователей,
изоляция задач и адаптация Windows-рендера; этот сервер наружу не публиковать.
