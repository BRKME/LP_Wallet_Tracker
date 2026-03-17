# LP Wallet Tracker 📊

Еженедельный мониторинг LP позиций с отчётами в Telegram.

## Функционал

- 📅 Еженедельные отчёты (воскресенье 10:00 MSK)
- 💰 План vs Факт по месяцам
- 📈 Изменения за неделю (абсолютные и %)
- 📆 Изменения за месяц (первая неделя каждого месяца)
- ✅ Проверка активов на соответствие белому списку
- 👛 Отслеживание двух кошельков: Марта + Аркаша

## Кошельки

| Имя | Адрес |
|-----|-------|
| Марта | `0x10082016a94920aBdf410CDB6f98c2Ead2c57340` |
| Аркаша | `0x305220d077474c5cab839E7C1cB3264Aca19f1B9` |

## Белый список активов

| Категория | Токены |
|-----------|--------|
| Layer 1 | BTC, ETH, BNB |
| Trading Infrastructure | HYPE, ASTER, PUMP |
| DeFi Credit | MORPHO |
| AI Narrative | TAO |
| Privacy Hedge | ZEC |
| Stablecoins | USDT, USDC, DAI, BUSD |

## Настройка

### Secrets (GitHub → Settings → Secrets)

```
TELEGRAM_TOKEN     - Токен бота
TELEGRAM_CHAT_ID   - ID чата для отправки
DEBANK_API_KEY     - API ключ DeBank (опционально)
```

### План по месяцам

Редактируйте `MONTHLY_PLAN` в `config.py`:

```python
MONTHLY_PLAN = {
    "2025-01": 5000,
    "2025-02": 6000,
    # ...
}
```

## Пример сообщения

```
📊 LP Portfolio Report
📅 17.03.2026

💰 Статус активов:
├ План: $19,000
├ Факт: $21,500
└ ✅ Разница: +$2,500 (+13.2%)

📈 Изменения за неделю:
└ +$800 (+3.9%)

👛 По кошелькам:
├ Марта: $10,500
├ Аркаша: $11,000

✅ Все активы в белом списке

🔗 DeBank Аркаша | DeBank Марта
```

## Запуск

```bash
# Установка
pip install -r requirements.txt

# Запуск
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export DEBANK_API_KEY="..."  # опционально
python main.py
```

## Data Source

Данные получаются через:
1. DeBank API (если есть ключ)
2. Ankr API (fallback)
