import asyncio
import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from ostium_python_sdk import OstiumSDK, NetworkConfig

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
RPC_URL = os.getenv('RPC_URL')
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
TARGET_WALLET = "0x7c930969fcf3e5a5c78bcf2e1cefda3f53e3c8fd"
# Telegram Group Chat ID (the negative number from the group)
TELEGRAM_GROUP_CHAT_ID = os.getenv('TELEGRAM_GROUP_CHAT_ID')
if TELEGRAM_GROUP_CHAT_ID:
    TELEGRAM_GROUP_CHAT_ID = int(TELEGRAM_GROUP_CHAT_ID)
# Topic ID where to send messages (required if using topics)
MESSAGE_THREAD_ID = os.getenv('MESSAGE_THREAD_ID')
if MESSAGE_THREAD_ID:
    MESSAGE_THREAD_ID = int(MESSAGE_THREAD_ID)

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress verbose GraphQL introspection logs
logging.getLogger('gql.transport.aiohttp').setLevel(logging.WARNING)

if not all([TELEGRAM_BOT_TOKEN, RPC_URL, PRIVATE_KEY, TELEGRAM_GROUP_CHAT_ID, MESSAGE_THREAD_ID]):
    logger.error("Missing environment variables. Please check .env file.")
    logger.error("Required: TELEGRAM_BOT_TOKEN, RPC_URL, PRIVATE_KEY, TELEGRAM_GROUP_CHAT_ID, MESSAGE_THREAD_ID")
    exit(1)

# --- Ostium Helper ---
async def get_current_trades_dict(sdk, retries=5):
    """Fetches open trades and returns a dict keyed by unique ID."""
    for attempt in range(retries):
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
            wait_time = min(2 ** attempt, 30)  # Exponential backoff: 1s, 2s, 4s, 8s, 16s (max 30s)
            logger.warning(f"Failed to fetch trades (attempt {attempt+1}/{retries}): {type(e).__name__}")
            if attempt < retries - 1:
                logger.info(f"Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"All {retries} attempts failed. Subgraph may be down: {e}")
                return None  # Return None to indicate failure, not empty dict

def format_trade_message(trade, status="OPEN", close_details=None):
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
        elif status == "CLOSED":
            # Check if this is a liquidation (no close details or no price)
            is_liquidation = False
            if not close_details:
                is_liquidation = True
            else:
                try:
                    close_price = float(close_details.get('price', 0))
                    if close_price == 0:
                        is_liquidation = True
                except:
                    is_liquidation = True

            # Set title based on liquidation status
            title = "💀 **LIQUIDATED** 💀" if is_liquidation else "❌ **TRADE CLOSED** ❌"

            base_msg = (
                f"{title}\n"
                f"**Pair:** {pair_symbol}\n"
                f"**Direction:** {direction_str}\n"
                f"**Entry Price:** {open_price_val:,.2f}\n"
                f"**Size:** {size_val:,.2f} USDC\n"
                f"**Collateral:** {collateral_val:,.2f} USDC\n"
                f"**Leverage:** {leverage_val:.2f}x\n"
                f"**Wallet:** `{TARGET_WALLET}`"
            )

            # Add close details only if not liquidated
            if close_details and not is_liquidation:
                try:
                    close_price_val = float(close_details.get('price', 0)) / 1e18

                    # PnL Calculation: Amount Sent - Collateral
                    amt_sent = float(close_details.get('amountSentToTrader', 0))
                    hist_collateral = float(close_details.get('collateral', 0))

                    pnl_val = (amt_sent - hist_collateral) / 1e6
                    pnl_sign = "+" if pnl_val >= 0 else ""
                    pnl_emoji = "✅" if pnl_val >= 0 else "❌"

                    additional_info = (
                        f"\n**Close Price:** {close_price_val:,.2f}\n"
                        f"**PnL:** {pnl_emoji} {pnl_sign}{pnl_val:,.2f} USDC"
                    )
                    return base_msg + additional_info
                except Exception as e:
                    logger.error(f"Error formatting close details: {e}")

            return base_msg
            
        return ""
    except Exception as e:
        logger.error(f"Error formatting trade: {e}")
        return f"Error formatting trade: {str(e)}"

# --- Telegram Handlers ---
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current open positions (command for manual check)."""
    try:
        config = NetworkConfig.mainnet()
        config.graph_url = "https://api.subgraph.ormilabs.com/api/public/67a599d5-c8d2-4cc4-9c4d-2975a97bc5d8/subgraphs/ost-prod/live/gn"
        sdk = OstiumSDK(config, PRIVATE_KEY, RPC_URL)
        trades = await get_current_trades_dict(sdk)

        if trades is None:
            await update.message.reply_text("⚠️ Could not fetch positions.", parse_mode='Markdown')
        elif not trades:
            await update.message.reply_text("ℹ️ No open positions.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"📊 **Current Open Positions ({len(trades)}):**", parse_mode='Markdown')
            for uid, trade in trades.items():
                msg = format_trade_message(trade, status="OPEN")
                await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error fetching status: {e}")
        await update.message.reply_text("⚠️ Error fetching positions.", parse_mode='Markdown')

async def broadcast_message(application: Application, message: str):
    """Sends a message to the configured group and topic."""
    try:
        kwargs = {
            "chat_id": TELEGRAM_GROUP_CHAT_ID,
            "text": message,
            "parse_mode": 'Markdown'
        }
        if MESSAGE_THREAD_ID:
            kwargs["message_thread_id"] = MESSAGE_THREAD_ID

        await application.bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"Failed to send message to group: {e}")

# --- Ostium Polling Task ---
async def poll_ostium(application: Application):
    """Polls Ostium SDK for trade updates."""
    logger.info(f"Starting Ostium Monitor for {TARGET_WALLET}...")

    try:
        config = NetworkConfig.mainnet()
        # Override the subgraph URL with the new Ormi endpoint
        config.graph_url = "https://api.subgraph.ormilabs.com/api/public/67a599d5-c8d2-4cc4-9c4d-2975a97bc5d8/subgraphs/ost-prod/live/gn"
        sdk = OstiumSDK(config, PRIVATE_KEY, RPC_URL)
        logger.info(f"SDK Initialized with Ormi subgraph: {config.graph_url}")
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
                logger.warning("Subgraph unavailable. Will retry in 5 minutes.")
                await asyncio.sleep(300)  # Wait 5 minutes when subgraph is down
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
                used_history_ids = set() # Track matched history items to avoid duplicates
                
                # Fetch history once if there are closed trades
                history = []
                closed_trades_list = [t for uid, t in known_trades.items() if uid not in current_trades]
                
                if closed_trades_list:
                    try:
                        # Fetch recent history
                        # Increase last_n_orders to ensure we catch multiple simultaneous closes
                        history = await sdk.subgraph.get_recent_history(TARGET_WALLET, last_n_orders=20)
                    except Exception as e:
                        logger.error(f"Failed to fetch history for closed trades: {e}")

                for uid, trade in known_trades.items():
                    if uid not in current_trades:
                        # Trade is CLOSED
                        logger.info(f"Trade {uid} closed. finding details...")
                        
                        close_details = None
                        
                        if history:
                            trade_pair_id = trade.get('pair', {}).get('id')
                            trade_collateral = float(trade.get('collateral', 0))
                            
                            best_match = None
                            min_diff = float('inf')
                            
                            for item in history:
                                # Skip if already matched to another trade in this batch
                                if item.get('id') in used_history_ids:
                                    continue
                                    
                                if (item.get('pair', {}).get('id') == trade_pair_id and 
                                    item.get('orderAction') == 'Close'):
                                    
                                    # Match by Collateral (within 1 USDC tolerance)
                                    # 1 USDC = 1,000,000 units
                                    hist_collateral = float(item.get('collateral', 0))
                                    diff = abs(hist_collateral - trade_collateral)
                                    
                                    if diff < 1000000: 
                                        if diff < min_diff:
                                            min_diff = diff
                                            best_match = item
                            
                            if best_match:
                                close_details = best_match
                                used_history_ids.add(best_match.get('id'))

                        # Format message with optional details
                        msg = format_trade_message(trade, status="CLOSED", close_details=close_details)
                        await broadcast_message(application, msg)
                        closed_uids.append(uid)
                
                for uid in closed_uids:
                    del known_trades[uid]

        except Exception as e:
            logger.error(f"Error during polling: {e}")
        
        await asyncio.sleep(60)

async def main():
    """Start the bot."""
    # Create custom HTTPXRequest with increased timeouts
    # Default timeout is often 5-10 seconds, we increase to 60 seconds
    http_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,  # 30 seconds for connection establishment
        read_timeout=60.0,      # 60 seconds for reading response
        write_timeout=30.0,     # 30 seconds for writing request
        pool_timeout=10.0       # 10 seconds for acquiring connection from pool
    )
    
    # Create the Application with custom request object
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(http_request).build()

    # Add handlers
    application.add_handler(CommandHandler("status", status))

    # Initialize application with retry logic
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"Initializing Telegram bot (attempt {attempt + 1}/{max_retries})...")
            await application.initialize()
            await application.start()
            logger.info("✅ Telegram bot initialized successfully!")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 5 * (attempt + 1)  # 5s, 10s, 15s
                logger.warning(f"Failed to initialize bot: {type(e).__name__}: {e}")
                logger.info(f"Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Failed to initialize bot after {max_retries} attempts. Exiting.")
                raise
    
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
