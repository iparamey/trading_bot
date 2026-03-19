# MT5 Hedging Grid Bot

Python-бот для сеточной hedging-стратегии (MetaTrader через MetaApi) с веб-интерфейсом на Streamlit.

## Что умеет

- Поддерживает сетку BUY/SELL по уровням (`GRID|...`), автоматическую синхронизацию ордеров и TP.
- Показывает в UI:
  - открытые позиции,
  - отложенные ордера,
  - закрытые прибыльные сделки (Executed Deals),
  - баланс/эквити и служебные логи.
- Позволяет вручную управлять ордерами из UI:
  - `Close All`,
  - `Close Selected Orders`,
  - установка TP для выбранных позиций,
  - `Reset Grid`.
- Имеет автообновление дашборда с возможностью:
  - поставить паузу (`Pause auto-refresh`),
  - изменить интервал обновления.

## Быстрый старт

1. Установите зависимости:

```bash
python3 -m pip install -r requirements.txt
```

2. Проверьте настройки в `config.yaml`:
   - блок `mt5` (логин/пароль/сервер/MetaApi token/account id),
   - блок `trading` (символ, magic, интервалы),
   - параметры `grid`, `closing`, `risk`.

## Запуск

### 1) Режим с UI (рекомендуется)

```bash
python3 -m streamlit run dashboard.py
```

После запуска откройте адрес из консоли (обычно `http://localhost:8501`) и нажмите **Start Bot**.

### 2) Headless (без UI)

```bash
python3 main_bot.py
```

С альтернативным конфигом:

```bash
python3 main_bot.py --config config.scalp.yaml
```

или

```bash
python3 main_bot.py --config config.calm.yaml
```

## Полезные файлы

- `main_bot.py` — торговая логика и цикл бота.
- `dashboard.py` — Streamlit UI.
- `mt5_utils.py` — слой интеграции с MetaApi/MT.
- `config.yaml` — основной конфиг.
- `logs/bot.log` — текстовый runtime-лог.
- `logs/order_actions.jsonl` — структурированные события по действиям с ордерами.
- `logs/critical_errors.jsonl` — критические ошибки.

## Замечания по безопасности

- Не коммитьте реальные токены/пароли из `config.yaml`.
- Для шаринга репозитория используйте шаблон конфига без секретов.
