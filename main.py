import asyncio
import os
import json
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Forbidden
from ostium_python_sdk import OstiumSDK, NetworkConfig

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
RPC_URL = os.getenv('RPC_URL')
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
TARGET_WALLET = "0x7c930969fcf3e5a5c78bcf2e1cefda3f53e3c8fd"
SUBSCRIBERS_FILE = "subscribers.json"

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

if not all([TELEGRAM_BOT_TOKEN, RPC_URL, PRIVATE_KEY]):
    logger.error("Missing environment variables. Please check .env file.")
    exit(1)

# --- Persistence ---
def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, 'r') as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()

def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, 'w') as f:
        json.dump(list(subscribers), f)

# Global set of subscribers
subscribers = load_subscribers()

# --- Ostium Helper ---
async def get_current_trades_dict(sdk):
    """Fetches open trades and returns a dict keyed by unique ID."""
    try:
        open_trades = await sdk.subgraph.get_open_trades(TARGET_WALLET)
        current_trades = {}
        for trade in open_trades:
            pair_id = trade.get('pair', {}).get('id')
            trade_index = trade.get('index')
            unique_id = f"{pair_id}-{trade_index}"
            current_trades[unique_id] = trade
        return current_trades
    except Exception as e:
        logger.error(f"Failed to fetch trades: {e}")
        return None  # Return None to indicate failure, not empty dict

def format_trade_message(trade, status="OPEN"):
    """Formats a trade dict into a readable string."""
    try:
        # Extract Pair
        pair_from = trade.get('pair', {}).get('from', 'Unknown')
        pair_to = trade.get('pair', {}).get('to', 'USD')
        pair_symbol = f"{pair_from}/{pair_to}"

        # Extract and Scale Values
        # USDC has 6 decimals
        collateral_raw = float(trade.get('collateral', 0))
        collateral_val = collateral_raw / 1e6
        
        notional_raw = float(trade.get('notional', 0))
        size_val = notional_raw / 1e6

        # Price usually has 18 decimals
        open_price_raw = float(trade.get('openPrice', 0))
        open_price_val = open_price_raw / 1e18

        # Leverage: 2500 likely means 25.00x (2 decimals)
        leverage_raw = float(trade.get('leverage', 0))
        leverage_val = leverage_raw # Display raw first, user can correct if it looks wrong. 
        # Actually, if collateral is 160k and size is 4M, leverage = 4M / 160k = 25.
        # So 2500 raw = 25x. Thus we divide by 100.
        leverage_val = leverage_raw / 100

        is_long = trade.get('isBuy', True) # 'isBuy' from raw data
        direction_str = "LONG 🟢" if is_long else "SHORT 🔴"
        
        if status == "OPEN":
            return (
                f"🟢 **OPEN POSITION**\n"
                f"**Pair:** {pair_symbol}\n"
                f"**Direction:** {direction_str}\n"
                f"**Entry Price:** {open_price_val:,.2f}\n"
                f"**Size:** {size_val:,.2f} USDC\n"
                f"**Collateral:** {collateral_val:,.2f} USDC\n"
                f"**Leverage:** {leverage_val:.2f}x\n"
                f"**Wallet:** `{TARGET_WALLET}`"
            )
        return ""
    except Exception as e:
        logger.error(f"Error formatting trade: {e}")
        return f"Error formatting trade: {str(e)}"

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Subscribe user to notifications and show current status."""
    chat_id = update.effective_chat.id
    
    # 1. Subscribe
    if chat_id not in subscribers:
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text(f"✅ You are now subscribed to Ostium trade alerts for wallet `{TARGET_WALLET}`!", parse_mode='Markdown')
        logger.info(f"New subscriber: {chat_id}")
    else:
        await update.message.reply_text("You are already subscribed. Checking for open positions...", parse_mode='Markdown')

    # 2. Show current positions
    # We need an SDK instance. Since we can't easily share the one from the loop, 
    # we'll create a temporary one or (better) use a global/shared one if possible.
    # For simplicity/robustness in this script, we'll create a quick instance.
    try:
        config = NetworkConfig.mainnet()
        sdk = OstiumSDK(config, PRIVATE_KEY, RPC_URL)
        trades = await get_current_trades_dict(sdk)
        
        if trades is None:
            await update.message.reply_text("⚠️ Could not fetch current positions at this moment. Will notify you of future trades.")
        elif not trades:
            await update.message.reply_text("ℹ️ No open positions found for this wallet right now.")
        else:
            await update.message.reply_text(f"📊 **Current Open Positions ({len(trades)}):**", parse_mode='Markdown')
            for uid, trade in trades.items():
                msg = format_trade_message(trade, status="OPEN")
                await update.message.reply_text(msg, parse_mode='Markdown')
                
    except Exception as e:
        logger.error(f"Error fetching initial status for {chat_id}: {e}")
        await update.message.reply_text("⚠️ Could not fetch current positions at this moment.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unsubscribe user from notifications."""
    chat_id = update.effective_chat.id
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_subscribers(subscribers)
        await update.message.reply_text("❌ You have unsubscribed from alerts.")
        logger.info(f"Subscriber removed: {chat_id}")
    else:
        await update.message.reply_text("You are not subscribed.")

async def broadcast_message(application: Application, message: str):
    """Sends a message to all subscribers."""
    if not subscribers:
        return

    # Create a copy to iterate safely
    for chat_id in list(subscribers):
        try:
            await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        except Forbidden:
            # User blocked the bot
            logger.warning(f"User {chat_id} blocked the bot. Removing from subscribers.")
            subscribers.remove(chat_id)
            save_subscribers(subscribers)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")

# --- Ostium Polling Task ---
async def poll_ostium(application: Application):
    """Polls Ostium SDK for trade updates."""
    logger.info(f"Starting Ostium Monitor for {TARGET_WALLET}...")
    
    try:
        config = NetworkConfig.mainnet()
        sdk = OstiumSDK(config, PRIVATE_KEY, RPC_URL)
        logger.info("SDK Initialized.")
    except Exception as e:
        logger.error(f"Error initializing SDK: {e}")
        return

    known_trades = {}
    first_run = True

    while True:
        try:
            # Fetch open trades
            current_trades = await get_current_trades_dict(sdk)
            
            # Skip this cycle if fetch failed
            if current_trades is None:
                logger.warning("Skipping this polling cycle due to fetch error")
                await asyncio.sleep(60)
                continue

            if first_run:
                logger.info(f"Initial check: Found {len(current_trades)} open trades.")
                for uid, trade in current_trades.items():
                    # Log details to console
                    msg = format_trade_message(trade, status="OPEN")
                    logger.info(f"  - {uid}: {msg.replace(chr(10), ' ')}") # Log as single line
                
                known_trades = current_trades
                first_run = False
            else:
                # Check for NEW and MODIFIED trades
                for uid, trade in current_trades.items():
                    if uid not in known_trades:
                        # NEW TRADE
                        msg = format_trade_message(trade, status="OPEN")
                        msg = msg.replace("🟢 **OPEN POSITION**", "🚨 **NEW TRADE DETECTED** 🚨")
                        await broadcast_message(application, msg)
                        known_trades[uid] = trade
                    else:
                        # CHECK FOR MODIFICATIONS
                        old_trade = known_trades[uid]
                        old_collateral = float(old_trade.get('collateral', 0))
                        new_collateral = float(trade.get('collateral', 0))
                        
                        # Compare raw values
                        if abs(new_collateral - old_collateral) > 1000: # Ignore tiny dust changes (e.g. < 0.001 USDC)
                            # Calculate diff
                            diff_val = (new_collateral - old_collateral) / 1e6
                            sign = "+" if diff_val > 0 else ""
                            diff_str = f"({sign}{diff_val:,.2f} USDC)"
                            
                            # Base message
                            base_msg = format_trade_message(trade, status="OPEN")
                            # Modify header and inject diff
                            final_msg = base_msg.replace("🟢 **OPEN POSITION**", "⚠️ **TRADE UPDATE** ⚠️")
                            # Inject diff into collateral line
                            # We know the line format is "**Collateral:** X,XXX.XX USDC"
                            # We can just append it to the message for simplicity or replace
                            final_msg += f"\n**Change:** {diff_str}"
                            
                            await broadcast_message(application, final_msg)
                            known_trades[uid] = trade # Update known state

                # Check for CLOSED trades
                closed_uids = []
                for uid, trade in known_trades.items():
                    if uid not in current_trades:
                        # Use old trade data for the message
                        msg = format_trade_message(trade, status="OPEN")
                        msg = msg.replace("🟢 **OPEN POSITION**", "❌ **TRADE CLOSED** ❌")
                        await broadcast_message(application, msg)
                        closed_uids.append(uid)
                
                for uid in closed_uids:
                    del known_trades[uid]

        except Exception as e:
            logger.error(f"Error during polling: {e}")
        
        await asyncio.sleep(60)

async def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))

    # Initialize application
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # Run Ostium polling in the background
    # We create a task for the polling loop
    polling_task = asyncio.create_task(poll_ostium(application))

    # Keep the main loop running
    # In a production environment, you might want better signal handling
    try:
        # Wait for the polling task (which runs forever)
        await polling_task
    except asyncio.CancelledError:
        logger.info("Stopping bot...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
