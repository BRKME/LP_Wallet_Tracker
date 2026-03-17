#!/usr/bin/env python3
"""
LP Wallet Tracker - Weekly portfolio monitoring
Sends Telegram reports with plan vs actual comparison
"""

import os
import json
import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
from pathlib import Path

from config import WALLETS, WHITELIST, WHITELIST_FLAT, get_current_plan, is_whitelisted, get_whitelist_category

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DEBANK_API_KEY = os.getenv('DEBANK_API_KEY', '')

# Files
STATE_DIR = Path("state")
HISTORY_FILE = STATE_DIR / "history.json"


class WalletTracker:
    def __init__(self):
        self.session = None
        STATE_DIR.mkdir(exist_ok=True)
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    # ============ DATA FETCHING ============
    
    async def get_wallet_balance_debank(self, address: str) -> dict:
        """Fetch wallet balance from DeBank API"""
        if not DEBANK_API_KEY:
            logger.warning("DEBANK_API_KEY not set")
            return None
        
        headers = {
            "AccessKey": DEBANK_API_KEY,
            "accept": "application/json"
        }
        
        try:
            # Get total balance
            url = f"https://pro-openapi.debank.com/v1/user/total_balance?id={address}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    total_usd = data.get('total_usd_value', 0)
                else:
                    logger.error(f"DeBank API error: {resp.status}")
                    return None
            
            # Get token list for whitelist check
            url = f"https://pro-openapi.debank.com/v1/user/all_token_list?id={address}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    tokens = await resp.json()
                else:
                    tokens = []
            
            return {
                "total_usd": total_usd,
                "tokens": tokens
            }
        
        except Exception as e:
            logger.error(f"Error fetching DeBank data: {e}")
            return None
    
    async def get_wallet_balance_ankr(self, address: str) -> dict:
        """Fallback: Fetch wallet balance from Ankr API (free)"""
        try:
            # Ankr Advanced API - free tier
            url = "https://rpc.ankr.com/multichain"
            payload = {
                "jsonrpc": "2.0",
                "method": "ankr_getAccountBalance",
                "params": {
                    "walletAddress": address,
                    "blockchain": ["eth", "bsc", "arbitrum", "polygon"]
                },
                "id": 1
            }
            
            async with self.session.post(url, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get('result', {})
                    total_usd = float(result.get('totalBalanceUsd', '0'))
                    assets = result.get('assets', [])
                    
                    tokens = []
                    for asset in assets:
                        tokens.append({
                            'symbol': asset.get('tokenSymbol', ''),
                            'amount': float(asset.get('balance', 0)),
                            'price': float(asset.get('tokenPrice', 0)),
                            'value': float(asset.get('balanceUsd', 0))
                        })
                    
                    return {
                        "total_usd": total_usd,
                        "tokens": tokens
                    }
        except Exception as e:
            logger.error(f"Error fetching Ankr data: {e}")
        
        return None
    
    async def get_wallet_balance(self, address: str) -> dict:
        """Get wallet balance, trying multiple sources"""
        # Try DeBank first
        result = await self.get_wallet_balance_debank(address)
        if result:
            return result
        
        # Fallback to Ankr
        logger.info("Falling back to Ankr API")
        result = await self.get_wallet_balance_ankr(address)
        if result:
            return result
        
        logger.error(f"Could not fetch balance for {address}")
        return {"total_usd": 0, "tokens": []}
    
    # ============ WHITELIST CHECK ============
    
    def check_whitelist(self, tokens: list) -> dict:
        """Check tokens against whitelist"""
        whitelisted = []
        not_whitelisted = []
        
        for token in tokens:
            symbol = token.get('symbol', '') or token.get('optimized_symbol', '')
            value = token.get('value', 0) or token.get('amount', 0) * token.get('price', 0)
            
            if value < 1:  # Skip dust
                continue
            
            if is_whitelisted(symbol):
                category = get_whitelist_category(symbol)
                whitelisted.append({
                    'symbol': symbol,
                    'value': value,
                    'category': category
                })
            else:
                not_whitelisted.append({
                    'symbol': symbol,
                    'value': value
                })
        
        return {
            'whitelisted': whitelisted,
            'not_whitelisted': not_whitelisted
        }
    
    # ============ HISTORY MANAGEMENT ============
    
    def load_history(self) -> dict:
        """Load historical data"""
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        return {"records": [], "monthly_snapshots": {}}
    
    def save_history(self, history: dict):
        """Save historical data"""
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    
    def add_record(self, history: dict, total_usd: float, plan_usd: float, details: dict):
        """Add a new record to history"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        record = {
            "date": today,
            "total_usd": total_usd,
            "plan_usd": plan_usd,
            "difference": total_usd - plan_usd,
            "wallets": details
        }
        
        history["records"].append(record)
        
        # Keep only last 52 weeks
        if len(history["records"]) > 52:
            history["records"] = history["records"][-52:]
        
        return record
    
    def get_last_week_data(self, history: dict) -> dict:
        """Get data from last week"""
        if len(history["records"]) >= 2:
            return history["records"][-2]
        return None
    
    def get_month_start_data(self, history: dict) -> dict:
        """Get data from start of current month"""
        current_month = datetime.now().strftime("%Y-%m")
        
        # Check monthly snapshots first
        if current_month in history.get("monthly_snapshots", {}):
            return history["monthly_snapshots"][current_month]
        
        # Find first record of the month
        for record in history["records"]:
            if record["date"].startswith(current_month):
                return record
        
        return None
    
    def save_monthly_snapshot(self, history: dict, record: dict):
        """Save snapshot at start of month"""
        current_month = datetime.now().strftime("%Y-%m")
        if "monthly_snapshots" not in history:
            history["monthly_snapshots"] = {}
        
        if current_month not in history["monthly_snapshots"]:
            history["monthly_snapshots"][current_month] = record
            logger.info(f"Saved monthly snapshot for {current_month}")
    
    # ============ MESSAGE FORMATTING ============
    
    def format_number(self, num: float) -> str:
        """Format number with commas"""
        return f"${num:,.0f}"
    
    def format_change(self, current: float, previous: float) -> str:
        """Format change with absolute and percentage"""
        diff = current - previous
        if previous > 0:
            pct = (diff / previous) * 100
            sign = "+" if diff >= 0 else ""
            return f"{sign}${diff:,.0f} ({sign}{pct:.1f}%)"
        return f"+${diff:,.0f}"
    
    def is_first_week_of_month(self) -> bool:
        """Check if it's the first week of the month"""
        today = datetime.now()
        return today.day <= 7
    
    def build_message(self, record: dict, last_week: dict, month_start: dict, whitelist_report: dict) -> str:
        """Build the Telegram message"""
        today = datetime.now().strftime("%d.%m.%Y")
        
        msg = f"📊 <b>LP Portfolio Report</b>\n"
        msg += f"📅 {today}\n\n"
        
        # Plan vs Fact
        msg += f"<b>💰 Статус активов:</b>\n"
        msg += f"├ План: {self.format_number(record['plan_usd'])}\n"
        msg += f"├ Факт: {self.format_number(record['total_usd'])}\n"
        
        diff = record['total_usd'] - record['plan_usd']
        diff_pct = (diff / record['plan_usd'] * 100) if record['plan_usd'] > 0 else 0
        emoji = "✅" if diff >= 0 else "⚠️"
        sign = "+" if diff >= 0 else ""
        msg += f"└ {emoji} Разница: {sign}${diff:,.0f} ({sign}{diff_pct:.1f}%)\n\n"
        
        # Weekly change
        if last_week:
            msg += f"<b>📈 Изменения за неделю:</b>\n"
            msg += f"└ {self.format_change(record['total_usd'], last_week['total_usd'])}\n\n"
        
        # Monthly change (first week only)
        if self.is_first_week_of_month() and month_start:
            msg += f"<b>📆 Изменения за месяц:</b>\n"
            msg += f"└ {self.format_change(record['total_usd'], month_start['total_usd'])}\n\n"
        
        # Per-wallet breakdown
        msg += f"<b>👛 По кошелькам:</b>\n"
        for name, data in record['wallets'].items():
            msg += f"├ {name}: {self.format_number(data['total_usd'])}\n"
        msg += "\n"
        
        # Whitelist check
        not_whitelisted = whitelist_report.get('not_whitelisted', [])
        if not_whitelisted:
            msg += f"<b>⚠️ Активы вне белого списка:</b>\n"
            for token in sorted(not_whitelisted, key=lambda x: x['value'], reverse=True)[:5]:
                msg += f"├ {token['symbol']}: {self.format_number(token['value'])}\n"
            if len(not_whitelisted) > 5:
                msg += f"└ ... и ещё {len(not_whitelisted) - 5}\n"
        else:
            msg += "✅ Все активы в белом списке\n"
        
        # Links
        msg += f"\n🔗 <a href='https://debank.com/profile/{WALLETS['Аркаша']}'>DeBank Аркаша</a>"
        msg += f" | <a href='https://debank.com/profile/{WALLETS['Марта']}'>DeBank Марта</a>"
        
        return msg
    
    # ============ TELEGRAM ============
    
    async def send_telegram(self, message: str):
        """Send message to Telegram"""
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Telegram credentials not configured")
            print(message)  # Print for testing
            return
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        try:
            async with self.session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("✅ Message sent to Telegram")
                else:
                    error = await resp.text()
                    logger.error(f"Telegram error: {error}")
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
    
    # ============ MAIN ============
    
    async def run(self):
        """Main execution"""
        logger.info("🚀 Starting LP Wallet Tracker")
        
        # Get current month for plan
        current_month = datetime.now().strftime("%Y-%m")
        plan_usd = get_current_plan(current_month)
        logger.info(f"📋 Plan for {current_month}: ${plan_usd:,}")
        
        # Fetch balances for all wallets
        total_usd = 0
        wallet_details = {}
        all_tokens = []
        
        for name, address in WALLETS.items():
            logger.info(f"📊 Fetching {name} ({address[:8]}...)")
            data = await self.get_wallet_balance(address)
            
            wallet_details[name] = {
                "address": address,
                "total_usd": data["total_usd"]
            }
            total_usd += data["total_usd"]
            all_tokens.extend(data.get("tokens", []))
            
            logger.info(f"   └ Balance: ${data['total_usd']:,.2f}")
        
        logger.info(f"💰 Total: ${total_usd:,.2f}")
        
        # Check whitelist
        whitelist_report = self.check_whitelist(all_tokens)
        
        # Load history and add record
        history = self.load_history()
        last_week = self.get_last_week_data(history)
        month_start = self.get_month_start_data(history)
        
        record = self.add_record(history, total_usd, plan_usd, wallet_details)
        
        # Save monthly snapshot if first week
        if self.is_first_week_of_month():
            self.save_monthly_snapshot(history, record)
        
        self.save_history(history)
        
        # Build and send message
        message = self.build_message(record, last_week, month_start, whitelist_report)
        await self.send_telegram(message)
        
        logger.info("✅ Done!")


async def main():
    async with WalletTracker() as tracker:
        await tracker.run()


if __name__ == "__main__":
    asyncio.run(main())
