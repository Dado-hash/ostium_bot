# Ostium Telegram Bot - Project Overview

## Purpose
A Telegram bot that monitors real-time trading activity of a specific Ostium wallet on Arbitrum and sends detailed notifications to subscribed users about:
- New trade positions opened
- Trade closures (with PnL)
- Position modifications (collateral changes, partial closes)

## Tech Stack
- **Language**: Python 3.10+
- **Main Libraries**:
  - `python-telegram-bot`: Telegram bot framework
  - `ostium-python-sdk`: Ostium protocol SDK for Arbitrum
  - `python-dotenv`: Environment variable management
  - `asyncio`: Async task management

## Architecture
- **main.py**: Single-file bot with:
  - Telegram handlers (`/start`, `/stop` commands)
  - Ostium polling task (60-second intervals)
  - Subscriber persistence (JSON file)
  - Trade state tracking and change detection

## Codebase Structure
```
ostium_bot/
├── main.py              # Main bot logic
├── subscribers.json     # Persistent subscriber list
├── .env                 # Environment configuration
├── requirements.txt     # Python dependencies
└── README.md           # Documentation
```

## Key Components
1. **Subscriber Management**: Dynamic subscription via `/start` and `/stop`
2. **Trade Monitoring**: Polls Ostium subgraph every 60 seconds
3. **State Tracking**: Detects new, modified, and closed trades
4. **Broadcasting**: Sends formatted trade alerts to all subscribers
5. **Error Handling**: Retry logic for API failures with exponential backoff