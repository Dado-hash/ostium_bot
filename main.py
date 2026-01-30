import asyncio
import os
import json
import logging
from datetime import datetime, time
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Forbidden
from telegram.request import HTTPXRequest
from ostium_python_sdk import OstiumSDK, NetworkConfig

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
RPC_URL = os.getenv('RPC_URL')
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
TARGET_WALLET = "0x7c930969fcf3e5a5c78bcf2e1cefda3f53e3c8fd"
SUBSCRIBERS_FILE = "subscribers.json"
# Telegram Group Chat ID (the negative number from the group)
TELEGRAM_GROUP_CHAT_ID = os.getenv('TELEGRAM_GROUP_CHAT_ID')
if TELEGRAM_GROUP_CHAT_ID:
    TELEGRAM_GROUP_CHAT_ID = int(TELEGRAM_GROUP_CHAT_ID)
# Topic ID where to send messages (required if using topics)
MESSAGE_THREAD_ID = os.getenv('MESSAGE_THREAD_ID')
if MESSAGE_THREAD_ID:
    MESSAGE_THREAD_ID = int(MESSAGE_THREAD_ID)
# Daily report time (format: HH:MM in 24h format, default 09:00)
DAILY_REPORT_TIME = os.getenv('DAILY_REPORT_TIME', '09:00')

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

# --- Fee Structure (in basis points - bps) ---
# Based on Ostium's fee schedule
OPENING_FEES = {
    # Commodities
    'XAU/USD': 3,   # Gold
    'CL/USD': 10,   # Oil
    'HG/USD': 15,   # Copper
    'XAG/USD': 15,  # Silver
    'XPT/USD': 20,  # Platinum
    'XPD/USD': 20,  # Palladium
    # Forex
    'USD/MXN': 5,   # Exception
    # Default rates by asset class (will be applied if specific pair not found)
    '_CRYPTO_': 10,  # Taker fee for crypto
    '_INDICES_': 5,  # Taker fee for indices
    '_FOREX_': 3,    # Taker fee for forex
    '_STOCKS_': 5,   # Taker fee for stocks
}

def get_opening_fee_bps(pair_symbol):
    """Returns the opening fee in basis points for a given pair."""
    # Check if specific pair has a fee
    if pair_symbol in OPENING_FEES:
        return OPENING_FEES[pair_symbol]

    # Default to crypto rate (most common, highest fee)
    # In production, you'd classify the pair by asset class
    return OPENING_FEES['_CRYPTO_']

def calculate_opening_fee(notional_usdc, pair_symbol):
    """Calculate opening fee in USDC based on notional value and pair."""
    fee_bps = get_opening_fee_bps(pair_symbol)
    # 1 bps = 0.01%
    fee_usdc = (notional_usdc * fee_bps) / 10000
    return fee_usdc

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

        # Calculate opening fee based on Ostium's fee structure
        opening_fee = calculate_opening_fee(size_val, pair_symbol)

        if status == "OPEN":
            msg = (
                f"🟢 **OPEN POSITION**\n"
                f"**Pair:** {pair_symbol}\n"
                f"**Direction:** {direction_str}\n"
                f"**Entry Price:** {open_price_val:,.2f}\n"
                f"**Size:** {size_val:,.2f} USDC\n"
                f"**Collateral:** {collateral_val:,.2f} USDC\n"
                f"**Leverage:** {leverage_val:.2f}x\n"
                f"**Opening Fee:** {opening_fee:,.2f} USDC\n"
                f"**Wallet:** `{TARGET_WALLET}`"
            )
            return msg
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

                    # Extract funding fees (rollover + funding) from close details
                    # These are in wei (18 decimals)
                    rollover_fee = float(close_details.get('rolloverFee', 0)) / 1e18
                    funding_fee = float(close_details.get('fundingFee', 0)) / 1e18
                    total_funding = rollover_fee + funding_fee

                    additional_info = f"\n**Close Price:** {close_price_val:,.2f}\n"
                    additional_info += f"**Opening Fee:** {opening_fee:,.2f} USDC\n"

                    # Show funding fees if significant (> $0.01)
                    if total_funding > 0.01:
                        additional_info += f"**Funding Paid:** {total_funding:,.2f} USDC\n"

                    additional_info += f"**PnL:** {pnl_emoji} {pnl_sign}{pnl_val:,.2f} USDC"

                    return base_msg + additional_info
                except Exception as e:
                    logger.error(f"Error formatting close details: {e}")

            return base_msg
            
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
    try:
        config = NetworkConfig.mainnet()
        config.graph_url = "https://api.subgraph.ormilabs.com/api/public/67a599d5-c8d2-4cc4-9c4d-2975a97bc5d8/subgraphs/ost-prod/live/gn"
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

async def get_current_price(sdk, pair_id):
    """Gets current market price for a pair."""
    try:
        # Get pair details which should include current price
        pair_details = await sdk.subgraph.get_pair_details(pair_id)
        # The price might be in different fields, we'll try common ones
        # For now return None, will need to check actual structure
        return None
    except Exception as e:
        logger.error(f"Error getting current price for pair {pair_id}: {e}")
        return None

async def calculate_unrealized_pnl(trade, current_price):
    """Calculate unrealized PNL for a single trade."""
    try:
        is_long = trade.get('isBuy', True)
        open_price = float(trade.get('openPrice', 0)) / 1e18
        notional = float(trade.get('notional', 0)) / 1e6
        collateral = float(trade.get('collateral', 0)) / 1e6

        if not current_price or open_price == 0:
            return 0.0

        # Calculate PNL based on price change
        # For LONG: PNL = (current_price - open_price) / open_price * notional
        # For SHORT: PNL = (open_price - current_price) / open_price * notional
        price_change_ratio = (current_price - open_price) / open_price if is_long else (open_price - current_price) / open_price
        pnl = price_change_ratio * notional

        return pnl
    except Exception as e:
        logger.error(f"Error calculating unrealized PNL: {e}")
        return 0.0

async def get_account_stats(sdk):
    """Fetches account statistics for daily report."""
    try:
        # Get open trades
        open_trades = await sdk.subgraph.get_open_trades(TARGET_WALLET)

        # Calculate unrealized PNL and total position value
        unrealized_pnl = 0.0
        total_position_value = 0.0
        positions_details = []

        for trade in open_trades:
            # Extract trade details
            pair_from = trade.get('pair', {}).get('from', 'Unknown')
            pair_to = trade.get('pair', {}).get('to', 'USD')
            pair_symbol = f"{pair_from}/{pair_to}"

            is_long = trade.get('isBuy', True)
            direction = "LONG" if is_long else "SHORT"
            direction_emoji = "🟢" if is_long else "🔴"

            leverage_raw = float(trade.get('leverage', 0))
            leverage = leverage_raw / 100

            notional = float(trade.get('notional', 0)) / 1e6
            total_position_value += notional

            # Try to get current price and calculate PNL
            pair_id = trade.get('pair', {}).get('id')
            pnl = 0.0
            if pair_id:
                current_price = await get_current_price(sdk, pair_id)
                pnl = await calculate_unrealized_pnl(trade, current_price)
                unrealized_pnl += pnl

            # Store position details
            positions_details.append({
                'pair': pair_symbol,
                'direction': direction,
                'direction_emoji': direction_emoji,
                'leverage': leverage,
                'size': notional,
                'pnl': pnl
            })

        return {
            'unrealized_pnl': unrealized_pnl,
            'total_position_value': total_position_value,
            'open_positions': len(open_trades),
            'positions': positions_details
        }
    except Exception as e:
        logger.error(f"Error fetching account stats: {e}")
        return None

def format_daily_report(stats):
    """Formats account statistics into a daily report message."""
    if not stats:
        return "⚠️ Could not generate daily report at this time."

    # Format numbers with proper signs and emojis
    unrealized_pnl = stats['unrealized_pnl']
    unrealized_sign = "+" if unrealized_pnl >= 0 else ""
    unrealized_emoji = "📈" if unrealized_pnl >= 0 else "📉"

    report = (
        f"📊 **DAILY ACCOUNT REPORT** 📊\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{unrealized_emoji} **Total Unrealized PNL:** {unrealized_sign}{unrealized_pnl:,.2f} USDC\n"
        f"💼 **Total Position Value:** {stats['total_position_value']:,.2f} USDC\n"
        f"📍 **Open Positions:** {stats['open_positions']}\n"
    )

    # Add individual positions if available
    positions = stats.get('positions', [])
    if positions:
        report += f"\n**Open Positions:**\n"
        for idx, pos in enumerate(positions, 1):
            pnl_sign = "+" if pos['pnl'] >= 0 else ""
            pnl_emoji = "✅" if pos['pnl'] >= 0 else "❌"

            report += (
                f"{idx}️⃣ **{pos['pair']}** {pos['direction']} {pos['direction_emoji']} "
                f"{pos['leverage']:.0f}x - "
                f"Size: {pos['size']:,.2f} USDC"
            )

            # Show PNL if calculated (not zero)
            if abs(pos['pnl']) > 0.01:
                report += f" - PNL: {pnl_emoji} {pnl_sign}{pos['pnl']:,.2f} USDC"

            report += "\n"

    report += (
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Wallet:** `{TARGET_WALLET}`"
    )

    return report

async def broadcast_message(application: Application, message: str):
    """Sends a message to the configured group and all subscribed users."""
    # 1. Send to group + topic
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

    # 2. Send to all subscribed private chats
    if not subscribers:
        return

    for chat_id in list(subscribers):
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown'
            )
        except Forbidden:
            # User blocked the bot
            logger.warning(f"User {chat_id} blocked the bot. Removing from subscribers.")
            subscribers.remove(chat_id)
            save_subscribers(subscribers)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")

# --- Daily Report Scheduler ---
async def daily_report_scheduler(application: Application):
    """Sends daily account report at configured time."""
    logger.info(f"Starting Daily Report Scheduler (Report time: {DAILY_REPORT_TIME})...")

    # Parse the configured time
    try:
        hour, minute = map(int, DAILY_REPORT_TIME.split(':'))
        target_time = time(hour, minute)
    except Exception as e:
        logger.error(f"Invalid DAILY_REPORT_TIME format: {DAILY_REPORT_TIME}. Using default 09:00")
        target_time = time(9, 0)

    # Initialize SDK for daily reports
    try:
        config = NetworkConfig.mainnet()
        config.graph_url = "https://api.subgraph.ormilabs.com/api/public/67a599d5-c8d2-4cc4-9c4d-2975a97bc5d8/subgraphs/ost-prod/live/gn"
        sdk = OstiumSDK(config, PRIVATE_KEY, RPC_URL)
    except Exception as e:
        logger.error(f"Error initializing SDK for daily reports: {e}")
        return

    last_report_date = None

    while True:
        try:
            now = datetime.now()
            current_time = now.time()
            current_date = now.date()

            # Check if it's time to send the report and we haven't sent it today
            if (current_time.hour == target_time.hour and
                current_time.minute == target_time.minute and
                last_report_date != current_date):

                logger.info("Generating daily account report...")

                # Get account stats
                stats = await get_account_stats(sdk)

                # Format and send report
                report = format_daily_report(stats)
                await broadcast_message(application, report)

                logger.info("Daily report sent successfully!")
                last_report_date = current_date

            # Sleep for 60 seconds before checking again
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Error in daily report scheduler: {e}")
            await asyncio.sleep(60)

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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
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

    # Run background tasks
    polling_task = asyncio.create_task(poll_ostium(application))
    daily_report_task = asyncio.create_task(daily_report_scheduler(application))

    logger.info("All background tasks started successfully!")

    # Keep the main loop running
    try:
        # Wait for all tasks (which run forever)
        await asyncio.gather(polling_task, daily_report_task)
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
