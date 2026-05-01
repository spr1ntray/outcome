# OUTCOME Testnet Bot

Бот для автоматизации [Outcome.xyz](https://testnet.outcome.xyz) testnet на сети **Arbitrum Sepolia (421614)**.

```
        _.---.._             _.---...__
     .-'   /\   \          .'  /\     /
     `.   (  )   \        /   (  )   /
       `.  \/   .'\      /`.   \/  .'
         ``---''   )    (   ``---''
                 .';.--.;`.
               .' /_...._\ `.
             .'   `.a  a.'   `.
            (        \/        )
             `.___..-'`-..___.`
                \          /
 BY SPRINTRAY    `-.____.-'    ESPECIALLY FOR MXW CHAT USERS
```

> Полная документация (установка, настройка, безопасность, FAQ) — в [GitBook](https://app.gitbook.com/o/yXhq7m5EOvoMxxxeQpNY/s/s6myxwtZldZWT8Mn9QQz/).

---

## Что умеет

1. **Фаусет** — получает тестовый USDT0 (POST `/faucet/mint`).
2. **Ставки** — открывает позиции на рандомные или конкретные события (BUY YES/NO).
3. **Холд + продажа** — держит позиции заданное время и закрывает их.
4. **Принудительная продажа** — закрывает все открытые позиции на всех аккаунтах.
5. **Парсинг** — собирает статистику по кошелькам.
6. **Шифрованная БД** — приватные ключи и прокси хранятся локально под паролем (`input/database.enc`).
7. **Многопоточка + прокси + капсолвер** — масштабируется на десятки аккаунтов.

---

## Требования

- **Python 3.10+**
- macOS / Windows
- (Опционально) HTTP/SOCKS5 прокси
- Ключ от [CapSolver](https://dashboard.capsolver.com/passport/register?inviteCode=ypCnEHZAzZeh) (для турнстайла Cloudflare)

---

## Установка

### macOS / Linux

```bash
git clone https://github.com/fallex/outcome-MXW.git
cd outcome-MXW
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
git clone https://github.com/fallex/outcome-MXW.git
cd outcome-MXW
python -m venv env
env\Scripts\activate
pip install -r requirements.txt
```

---

## Настройка

### 1. Расходники

- **Прокси** — `maxy.tools` → раздел «Прокси».
- **Капсолвер** — регистрация по [реф-ссылке](https://dashboard.capsolver.com/passport/register?inviteCode=ypCnEHZAzZeh), затем Dashboard → API Key.

### 2. Файлы

```bash
# Переименуй шаблоны
mv input/private_keys.txt.example input/private_keys.txt
mv input/proxies.txt.example      input/proxies.txt
```

- В `input/private_keys.txt` — приватники, по одному на строку.
- В `input/proxies.txt` — прокси в формате `http://user:pass@host:port`, по одной на строку.

### 3. parameters.py

Открой `parameters.py` и пропиши:

- `CAPSOLVER_API_KEY` — твой ключ.
- `THREADS` — потоки (по умолчанию 12).
- `FAUCET_AMOUNT_RANGE`, `BET_*`, `HOLD_TIME_RANGE` — суммы и таймеры.

Все параметры подробно прокомментированы прямо в файле.

---

## Запуск

```bash
python main.py
```

Меню (стрелки ↑↓ + Enter):

1. **Создать базу данных** — зашифровать приватники паролем (делается **один раз**).
2. **Полный цикл** — фаусет → ставки → холд → продажа.
3. **Только активности** — без фаусета.
4. **Только пополнение** — только фаусет.
5. **Продать все позиции (принудительно)** — экстренное закрытие.
6. **Парсинг кошельков** — статистика.

---

## Безопасность

- Приватники **никогда** не лежат в открытом виде после создания БД — они зашифрованы паролем (AES) в `input/database.enc`. Файлы `private_keys.txt` и `proxies.txt` автоматически очищаются.
- `.gitignore` покрывает все чувствительные файлы (БД, логи, venv, .DS_Store).
- **Никогда не используй основной кошелёк.** Заводи отдельные адреса для софтов.
- **Никогда не клади seed-фразу на ПК.**

Подробнее — в [GitBook → Безопасность](https://app.gitbook.com/o/yXhq7m5EOvoMxxxeQpNY/s/s6myxwtZldZWT8Mn9QQz/).

---

## Поддержка

Вопросы и баги — пиши **sprintray** в приватный чат **MXW**.

---

## Дисклеймер

Софт предоставляется «как есть», под лицензией **MIT** (см. `LICENSE`). Использование тестнетов и любые активности с криптовалютами вы совершаете на свой страх и риск. Автор не несёт ответственности за последствия использования.

Код полностью открыт — каждый может убедиться, что софт чистый.
