# Ubuntu 24.04: запуск без PowerPoint

LibreOffice headless → PDF → Poppler PNG. Графическая сессия не нужна.
Рендер может отличаться от PowerPoint; установите исходные шрифты и проверьте
превью вашего шаблона. Исходный PPTX не изменяется. Макеты/семантика извлекаются
по-прежнему из PPTX, а PDF служит только промежуточным файлом для изображения.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git libreoffice-impress poppler-utils fonts-dejavu-core fonts-liberation
git clone https://github.com/daniilboriskin21-eng/pptx-template-parser.git
cd pptx-template-parser
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp config.example.json config.local.json
```

Отредактируйте base_url/model/api_key_env для вашего VLM. Для API можно добавить
`"renderer": "libreoffice"`; по умолчанию auto выбирает LibreOffice на Linux,
PowerPoint на Windows. Установка renderer не меняет модель и не снимает 429.

```bash
export VLM_API_KEY='ваш ключ'
python -m unittest discover -s tests -v
python -m template_parser.cli run /path/input.pptx --package output/template --config config.local.json --renderer libreoffice
```

Для проверки только рендера без расходов API:

```bash
python -m template_parser.cli extract /path/input.pptx --output output/render_test
python -m template_parser.cli render output/render_test --renderer libreoffice
```

Существующие previews не перезаписываются. Чтобы сравнить PowerPoint и LibreOffice,
используйте разные рабочие директории. При смене превью старые semantics перестают
соответствовать хешу картинки. Не копируйте их как будто рендер не изменился.

## API на Ubuntu

```bash
export PARSER_API_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python -m template_parser.cli serve --config config.local.json --storage output/api_jobs --origin https://frontend.example.com
```

Сервер слушает 127.0.0.1:8765. Для доступа команды настройте reverse proxy на этом
же хосте (TLS, лимит тела 50 MiB, Content-Length, без buffering в chunked при
передаче приложению). Не публикуйте stdlib-сервер напрямую; это интеграционный
однопользовательский API. Для production нужны авторизация пользователей, права
на задачи, изоляция LibreOffice, ограничения CPU/RAM/диска и очередь с восстановлением.
API контракт и пример fetch: FRONTEND_API.md. CLI не зависит от reverse proxy.

## Пример systemd (адаптируйте пути и пользователя)

Разместите проект в /opt/pptx-parser, создайте непривилегированного пользователя
parser с доступным HOME и правом записи в /var/lib/pptx-parser. Не запускайте от root.
Секреты храните в /etc/pptx-parser.env с правами 600, например VLM_API_KEY=...
и PARSER_API_TOKEN=...; config.local.json содержит имя переменной, а не её значение.

```ini
[Unit]
Description=PPTX parser integration API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=parser
WorkingDirectory=/opt/pptx-parser
EnvironmentFile=/etc/pptx-parser.env
Environment=HOME=/var/lib/pptx-parser
ExecStart=/opt/pptx-parser/.venv/bin/python -m template_parser.cli serve --config /opt/pptx-parser/config.local.json --storage /var/lib/pptx-parser/jobs --origin https://frontend.example.com
Restart=on-failure
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

После установки unit: systemctl daemon-reload, enable --now; логи journalctl -u
имя.service. Прерванные задачи после рестарта получают interrupted; продолжение
явным POST resume. Политики удаления версий и глобальных блокировок пока нет.

## Проверенность

Добавлен mock-тест конвертера и реальный Linux-тест двух слайдов, включая скрытый.
GitHub Actions ubuntu-24.04 устанавливает LibreOffice/Poppler и запускает тесты.
На Windows реальный Linux-тест пропускается. Успех CI нужно подтвердить после push;
наличие workflow само по себе не означает, что Ubuntu-развёртывание проверено.
PDF export options: https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html
