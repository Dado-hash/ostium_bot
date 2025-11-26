# Ostium Bot Setup Guide

This guide will help you set up and run the Ostium Telegram Bot.
This version supports multiple subscribers via `/start` and `/stop` commands.

## Prerequisites

1.  **Python 3.10+** installed.
2.  **Telegram Bot Token**: Create a bot via [@BotFather](https://t.me/BotFather) on Telegram.
3.  **Arbitrum RPC URL**: You can use a public one like `https://arb1.arbitrum.io/rpc` or get a private one from Alchemy/Infura.
4.  **Private Key**: A private key is required by the SDK initialization. You can use a new/empty wallet's private key.

## Installation

1.  Navigate to the bot directory:
    ```bash
    cd /Users/davide/Documents/scripts/ostium_bot
    ```

2.  Create a virtual environment (optional but recommended):
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

## Configuration

1.  Copy the example environment file:
    ```bash
    cp .env.example .env
    ```

2.  Edit `.env` and fill in your details:
    ```ini
    TELEGRAM_BOT_TOKEN=123456789:ABCdef...
    RPC_URL=https://arb1.arbitrum.io/rpc
    PRIVATE_KEY=0x...
    # Note: TELEGRAM_CHAT_ID is no longer needed in .env as users subscribe dynamically.
    ```

## Running the Bot

Start the bot with:
```bash
python main.py
```

## Usage

1.  **Start the Bot**: Run the script on your server/computer.
2.  **Subscribe**: Open your bot in Telegram and send `/start`.
3.  **Unsubscribe**: Send `/stop` to stop receiving alerts.
4.  **Groups**: Add the bot to a group and send `/start` in the group to subscribe the whole group.

The bot will save the list of subscribers to `subscribers.json` automatically.
