#!/usr/bin/env python3
"""
Telegram Admin Bot - XController
A Telegram bot for group administration with the following features:
1. Configurable auto-kick new members without username
2. Filter banned words from messages
3. Clean up deleted accounts
4. Progressive enforcement for banned words (delete -> ban)
"""

import os
import re
import logging
import asyncio
import sqlite3
import hashlib
import hmac
import time
from typing import Dict, Set, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import (
    ChatBannedRights, 
    User,
    MessageService,
    MessageActionChatAddUser,
    MessageActionChatJoinedByLink
)
from telethon.errors import (
    ChatAdminRequiredError,
    UserAdminInvalidError,
    FloodWaitError
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup data directory with fallback
def get_data_dir() -> Path:
    """Get data directory with fallback logic"""
    data_dir = Path("/data")
    if data_dir.exists() and data_dir.is_dir():
        try:
            # Test if we can write to /data
            test_file = data_dir / ".write_test"
            test_file.touch()
            test_file.unlink()
            return data_dir
        except (PermissionError, OSError):
            pass
    
    # Fallback to ./data
    fallback_dir = Path("./data")
    fallback_dir.mkdir(exist_ok=True)
    return fallback_dir

DATA_DIR = get_data_dir()

# Configure logging
log_file = DATA_DIR / "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.info(f"Using data directory: {DATA_DIR}")

class TokenBucket:
    """Token bucket implementation for rate limiting"""
    def __init__(self, capacity: int = 10, refill_rate: float = 2.0):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.time()

    async def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens, return True if successful"""
        now = time.time()
        # Add tokens based on time passed
        time_passed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + (time_passed * self.refill_rate))
        self.last_refill = now
        
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    async def wait_for_token(self):
        """Wait until a token is available"""
        while not await self.consume():
            await asyncio.sleep(0.1)

class DatabaseManager:
    """Manage SQLite database operations"""
    def __init__(self, db_path: Path, salt: str):
        self.db_path = db_path
        self.salt = salt.encode()
        self.init_database()
    
    def hash_user_id(self, user_id: int) -> str:
        """Hash user ID using HMAC-SHA256"""
        return hmac.new(self.salt, str(user_id).encode(), hashlib.sha256).hexdigest()
    
    def init_database(self):
        """Initialize database tables"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Violations table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS violations (
                    user_hash TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    last_violation TIMESTAMP
                )
            ''')
            
            # Global bans table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS global_bans (
                    user_hash TEXT PRIMARY KEY,
                    banned_at TIMESTAMP,
                    reason TEXT
                )
            ''')
            
            # Forward state table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS forward_state (
                    user_hash TEXT PRIMARY KEY,
                    last_forward TIMESTAMP
                )
            ''')
            
            # Cleanup state table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cleanup_state (
                    group_id TEXT PRIMARY KEY,
                    last_cleanup TIMESTAMP,
                    last_offset INTEGER DEFAULT 0
                )
            ''')
            
            conn.commit()
    
    def get_user_violations(self, user_id: int) -> int:
        """Get violation count for user"""
        user_hash = self.hash_user_id(user_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT count FROM violations WHERE user_hash = ?', (user_hash,))
            result = cursor.fetchone()
            return result[0] if result else 0
    
    def add_violation(self, user_id: int) -> int:
        """Add violation for user and return new count"""
        user_hash = self.hash_user_id(user_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO violations (user_hash, count, last_violation)
                VALUES (?, COALESCE((SELECT count FROM violations WHERE user_hash = ?), 0) + 1, ?)
            ''', (user_hash, user_hash, datetime.now()))
            conn.commit()
            return self.get_user_violations(user_id)
    
    def is_globally_banned(self, user_id: int) -> bool:
        """Check if user is globally banned"""
        user_hash = self.hash_user_id(user_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM global_bans WHERE user_hash = ?', (user_hash,))
            return cursor.fetchone() is not None
    
    def add_global_ban(self, user_id: int, reason: str = "Multiple violations"):
        """Add user to global ban list"""
        user_hash = self.hash_user_id(user_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO global_bans (user_hash, banned_at, reason)
                VALUES (?, ?, ?)
            ''', (user_hash, datetime.now(), reason))
            conn.commit()
    
    def can_forward(self, user_id: int) -> bool:
        """Check if user can forward (24h cooldown)"""
        user_hash = self.hash_user_id(user_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT last_forward FROM forward_state WHERE user_hash = ?', (user_hash,))
            result = cursor.fetchone()
            if not result:
                return True
            
            last_forward = datetime.fromisoformat(result[0])
            return datetime.now() - last_forward >= timedelta(hours=24)
    
    def update_forward_time(self, user_id: int):
        """Update last forward time for user"""
        user_hash = self.hash_user_id(user_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO forward_state (user_hash, last_forward)
                VALUES (?, ?)
            ''', (user_hash, datetime.now()))
            conn.commit()
    
    def get_cleanup_state(self, group_id: int) -> tuple:
        """Get cleanup state for group"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT last_cleanup, last_offset FROM cleanup_state WHERE group_id = ?', (str(group_id),))
            result = cursor.fetchone()
            if not result:
                return None, 0
            
            last_cleanup = datetime.fromisoformat(result[0]) if result[0] else None
            return last_cleanup, result[1]
    
    def update_cleanup_state(self, group_id: int, offset: int):
        """Update cleanup state for group"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO cleanup_state (group_id, last_cleanup, last_offset)
                VALUES (?, ?, ?)
            ''', (str(group_id), datetime.now(), offset))
            conn.commit()

class TelegramAdminBot:
    def __init__(self):
        # Environment variables
        self.api_id = os.getenv('API_ID')
        self.api_hash = os.getenv('API_HASH')
        self.bot_token = os.getenv('BOT_TOKEN')
        self.salt = os.getenv('SALT')
        
        # Username enforcement configuration
        self.enforce_username = os.getenv('ENFORCE_USERNAME', '1').lower() in ('1', 'true', 'yes', 'on')
        self.username_kick_notice = os.getenv('USERNAME_KICK_NOTICE', '')
        
        # Banned words from environment
        banned_words_str = os.getenv('BANNED_WORDS', '')
        self.banned_words = set(word.strip().lower() for word in banned_words_str.split(',') if word.strip())
        
        # Manual forward group IDs from environment
        forward_group_ids_str = os.getenv('FORWARD_GROUP_IDS', '')
        self.manual_forward_groups = []
        if forward_group_ids_str.strip():
            try:
                # Parse comma-separated list of integers
                group_ids = [int(gid.strip()) for gid in forward_group_ids_str.split(',') if gid.strip()]
                self.manual_forward_groups = group_ids
                logger.info(f"Loaded {len(self.manual_forward_groups)} forward group IDs from env (manual mode)")
            except ValueError as e:
                logger.error(f"Invalid FORWARD_GROUP_IDS format: {e}. Using auto-discovery instead.")
                self.manual_forward_groups = []
        
        # Validate required environment variables
        if not all([self.api_id, self.api_hash, self.bot_token, self.salt]):
            raise ValueError("Missing required environment variables: API_ID, API_HASH, BOT_TOKEN, SALT")
        
        # Initialize database
        db_path = DATA_DIR / "bot.db"
        self.db = DatabaseManager(db_path, self.salt)
        
        # Initialize rate limiter
        self.rate_limiter = TokenBucket(capacity=10, refill_rate=2.0)
        
        # Initialize Telethon client with data directory
        session_path = DATA_DIR / "bot_session"
        self.client = TelegramClient(str(session_path), int(self.api_id), self.api_hash)
        
        # Track groups for forwarding (max 20)
        self.forward_groups: List[int] = []
        
        logger.info("Bot initialized successfully")
    
    async def start(self):
        """Start the bot and register event handlers"""
        await self.client.start(bot_token=self.bot_token)
        logger.info("Bot started successfully")
        
        # Setup forwarding groups (manual or auto-discovery)
        await self.discover_forward_groups()
        
        # Register event handlers
        self.client.add_event_handler(self.handle_new_member, events.ChatAction)
        self.client.add_event_handler(self.handle_message, events.NewMessage)
        
        # Schedule maintenance tasks
        asyncio.create_task(self.maintenance_loop())
        
        logger.info("Event handlers registered, bot is running...")
    
    async def discover_forward_groups(self):
        """Setup groups for forwarding (manual or auto-discovery)"""
        # Use manual group IDs if provided
        if self.manual_forward_groups:
            self.forward_groups = self.manual_forward_groups.copy()
            logger.info(f"Using manual forward groups: {len(self.forward_groups)} groups configured")
            logger.info("Skipping auto-discovery because FORWARD_GROUP_IDS provided")
            return
        
        # Fallback to auto-discovery with best-effort error handling
        logger.info("FORWARD_GROUP_IDS not provided, attempting auto-discovery as fallback")
        self.forward_groups = []
        try:
            async for dialog in self.client.iter_dialogs():
                if (dialog.is_group or dialog.is_channel) and len(self.forward_groups) < 20:
                    try:
                        # Check if bot has permissions to send messages
                        await self.client.get_permissions(dialog.id, 'me')
                        self.forward_groups.append(dialog.id)
                    except Exception:
                        # Skip groups where bot doesn't have permissions
                        continue
            
            logger.info(f"Auto-discovered {len(self.forward_groups)} groups for forwarding")
        except Exception as e:
            logger.error(f"Error in auto-discovery (non-fatal): {e}")
            logger.info("Bot will continue without forwarding groups")
    
    async def handle_new_member(self, event):
        """Handle new members joining the group"""
        try:
            # Check if this is a user join event
            if not hasattr(event, 'action_message') or not event.action_message:
                return
            
            action = event.action_message.action
            
            # Get the users who joined
            users = []
            if hasattr(action, 'users'):
                # Users were added by someone
                users = action.users
            elif hasattr(event.action_message, 'from_id') and event.action_message.from_id:
                # User joined by link
                users = [event.action_message.from_id.user_id if hasattr(event.action_message.from_id, 'user_id') else event.action_message.from_id]
            else:
                return
            
            for user_id in users:
                try:
                    # Use get_entity only as requested (lightweight approach)
                    user = await self.client.get_entity(user_id)
                    
                    # Skip if it's a bot
                    if hasattr(user, 'bot') and user.bot:
                        continue
                    
                    # Check if user has a username - only enforce if enabled
                    if not hasattr(user, 'username') or not user.username:
                        if self.enforce_username:
                            # Send optional notice before kick (best-effort)
                            if self.username_kick_notice:
                                try:
                                    await self.client.send_message(
                                        event.chat_id, 
                                        self.username_kick_notice, 
                                        reply_to=event.action_message.id
                                    )
                                except Exception:
                                    # Ignore failures as requested (best-effort)
                                    pass
                            
                            # Kick user using rpc_call wrapper for consistent flood handling
                            await self.rpc_call(
                                "username_kick", 
                                self.client.edit_permissions, 
                                event.chat_id, 
                                user.id, 
                                view_messages=False
                            )
                            
                            # Log structured line as requested
                            logger.info(f"[USERNAME ENFORCE] kicked user_id={user.id} group={event.chat_id}")
                        else:
                            logger.info(f"User {user.id} ({getattr(user, 'first_name', 'Unknown')}) joined without username - allowed (enforcement disabled)")
                    else:
                        logger.info(f"User {user.username} joined and has username - allowed")
                        
                except Exception as e:
                    logger.error(f"Error processing new member {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error in handle_new_member: {e}")
    
    async def handle_message(self, event):
        """Handle new messages and check for banned words"""
        try:
            # Skip if message is from a bot, service message, or has no sender
            if (not hasattr(event.message, 'from_id') or 
                not event.message.from_id or 
                hasattr(event.message, 'service')):
                return
            
            # Get user ID safely
            user_id = None
            if hasattr(event.message.from_id, 'user_id'):
                user_id = event.message.from_id.user_id
            elif isinstance(event.message.from_id, int):
                user_id = event.message.from_id
            else:
                return
            
            # Check if user is globally banned
            if self.db.is_globally_banned(user_id):
                await self.rate_limited_ban_user(event.chat_id, user_id)
                return
            
            message_text = event.message.text
            
            if not message_text:
                return
            
            # Handle /id command
            if message_text.strip().lower() == '/id':
                try:
                    await event.reply(f"Chat ID: {event.chat_id}")
                    return
                except Exception as e:
                    logger.error(f"Error responding to /id command: {e}")
                    return
            
            # Check for banned words
            if self.contains_banned_words(message_text):
                logger.info(f"Banned word detected in message from user {user_id}")
                
                # Delete the message
                await event.delete()
                
                # Track violations in database
                violation_count = self.db.add_violation(user_id)
                
                if violation_count >= 2:
                    # Second violation - global ban
                    logger.info(f"Globally banning user {user_id} for repeated banned word usage")
                    self.db.add_global_ban(user_id, "Multiple banned word violations")
                    await self.propagate_global_ban(user_id)
                else:
                    # First violation - just delete message
                    logger.info(f"First violation for user {user_id} - message deleted")
            else:
                # Forward message if conditions are met
                await self.handle_message_forwarding(event, user_id, message_text)
                    
        except Exception as e:
            logger.error(f"Error in handle_message: {e}")
    
    async def handle_message_forwarding(self, event, user_id: int, message_text: str):
        """Handle message forwarding logic"""
        try:
            # Only forward plain text messages (no media)
            if not message_text or event.message.media:
                return
            
            # Check if user can forward (24h cooldown)
            if not self.db.can_forward(user_id):
                return
            
            # Skip if message contains banned words
            if self.contains_banned_words(message_text):
                return
            
            # Forward to all available groups (up to 20)
            forwarded_count = 0
            for group_id in self.forward_groups:
                if group_id == event.chat_id:
                    continue  # Don't forward to the same group
                
                try:
                    await self.rate_limiter.wait_for_token()
                    await self.client.send_message(group_id, message_text)
                    forwarded_count += 1
                    await asyncio.sleep(0.5)  # Additional rate limiting
                except Exception as e:
                    logger.error(f"Error forwarding to group {group_id}: {e}")
            
            if forwarded_count > 0:
                logger.info(f"Forwarded message from user {user_id} to {forwarded_count} groups")
                self.db.update_forward_time(user_id)
        
        except Exception as e:
            logger.error(f"Error in message forwarding: {e}")
    
    async def propagate_global_ban(self, user_id: int):
        """Propagate global ban to all groups"""
        banned_count = 0
        for group_id in self.forward_groups:
            try:
                await self.rate_limited_ban_user(group_id, user_id)
                banned_count += 1
            except Exception as e:
                logger.error(f"Error banning user {user_id} in group {group_id}: {e}")
        
        logger.info(f"Propagated global ban for user {user_id} to {banned_count} groups")
    
    def contains_banned_words(self, text: str) -> bool:
        """Check if text contains any banned words"""
        if not text or not self.banned_words:
            return False
        
        text_lower = text.lower()
        
        # Check for exact word matches
        words = re.findall(r'\b\w+\b', text_lower)
        for word in words:
            if word in self.banned_words:
                return True
        
        # Check for substring matches
        for banned_word in self.banned_words:
            if banned_word in text_lower:
                return True
        
        return False
    
    async def kick_user(self, chat_id: int, user_id: int):
        """Kick a user from the group"""
        try:
            # Ban user temporarily (this kicks them)
            ban_rights = ChatBannedRights(
                until_date=datetime.now() + timedelta(seconds=30),
                view_messages=True
            )
            await self.client.edit_permissions(chat_id, user_id, ban_rights)
            
            # Immediately unban them (so they can rejoin later)
            await asyncio.sleep(1)
            await self.client.edit_permissions(chat_id, user_id, None)
            
        except (ChatAdminRequiredError, UserAdminInvalidError):
            logger.error(f"Bot lacks admin permissions to kick user {user_id}")
        except FloodWaitError as e:
            logger.warning(f"Flood wait error, waiting {e.seconds} seconds")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error kicking user {user_id}: {e}")

    async def rpc_call(self, operation_name: str, func, *args, **kwargs):
        """Wrapper for RPC calls with flood handling"""
        try:
            return await func(*args, **kwargs)
        except FloodWaitError as e:
            logger.warning(f"Flood wait for {operation_name}: waiting {e.seconds} seconds")
            await asyncio.sleep(e.seconds)
            # Retry once after flood wait
            try:
                return await func(*args, **kwargs)
            except Exception as retry_e:
                logger.error(f"Error in {operation_name} after flood wait: {retry_e}")
                raise
        except (ChatAdminRequiredError, UserAdminInvalidError):
            logger.error(f"Bot lacks admin permissions for {operation_name}")
            raise
        except Exception as e:
            logger.error(f"Error in {operation_name}: {e}")
            raise

    async def rate_limited_ban_user(self, chat_id: int, user_id: int):
        """Ban a user with rate limiting"""
        await self.rate_limiter.wait_for_token()
        await self.ban_user(chat_id, user_id)
    
    async def ban_user(self, chat_id: int, user_id: int):
        """Ban a user from the group permanently"""
        try:
            ban_rights = ChatBannedRights(
                until_date=None,  # Permanent ban
                view_messages=True,
                send_messages=True,
                send_media=True,
                send_stickers=True,
                send_gifs=True,
                send_games=True,
                send_inline=True,
                embed_links=True
            )
            await self.client.edit_permissions(chat_id, user_id, ban_rights)
            
        except (ChatAdminRequiredError, UserAdminInvalidError):
            logger.error(f"Bot lacks admin permissions to ban user {user_id}")
        except FloodWaitError as e:
            logger.warning(f"Flood wait error, waiting {e.seconds} seconds")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error banning user {user_id}: {e}")
    
    async def cleanup_deleted_accounts(self, chat_id: int):
        """Remove deleted accounts from the group with pagination"""
        try:
            last_cleanup, offset = self.db.get_cleanup_state(chat_id)
            
            # Get participants with pagination (25 per run)
            participants = await self.client.get_participants(
                chat_id, 
                limit=25, 
                offset=offset
            )
            
            deleted_count = 0
            for participant in participants:
                # Check if user is deleted by checking if user.deleted attribute exists and is True
                if hasattr(participant, 'deleted') and participant.deleted:
                    try:
                        logger.info(f"Removing deleted account: {participant.id}")
                        await self.rate_limiter.wait_for_token()
                        await self.kick_user(chat_id, participant.id)
                        deleted_count += 1
                        await asyncio.sleep(1)  # Additional rate limiting
                    except Exception as e:
                        logger.error(f"Error removing deleted account {participant.id}: {e}")
            
            # Update cleanup state
            new_offset = offset + len(participants) if len(participants) == 25 else 0
            self.db.update_cleanup_state(chat_id, new_offset)
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} deleted accounts from group {chat_id}")
                        
        except Exception as e:
            logger.error(f"Error in cleanup_deleted_accounts: {e}")
    
    async def maintenance_loop(self):
        """Run maintenance tasks (cleanup every 12 hours)"""
        while True:
            try:
                # Wait 12 hours
                await asyncio.sleep(12 * 3600)
                
                logger.info("Starting maintenance loop")
                
                # Clean up all groups the bot is in (one group per run, rotating)
                if self.forward_groups:
                    # Rotate through groups, one group per maintenance cycle
                    group_count = len(self.forward_groups)
                    if group_count > 0:
                        # Get current time to determine which group to clean
                        current_time = int(time.time())
                        group_index = (current_time // (12 * 3600)) % group_count
                        group_to_clean = self.forward_groups[group_index]
                        
                        logger.info(f"Cleaning group {group_to_clean} (index {group_index})")
                        await self.cleanup_deleted_accounts(group_to_clean)
                
                logger.info("Maintenance loop completed")
                                
            except Exception as e:
                logger.error(f"Error in maintenance_loop: {e}")
    
    async def run(self):
        """Run the bot indefinitely"""
        await self.start()
        await self.client.run_until_disconnected()

async def main():
    """Main function to run the bot"""
    try:
        bot = TelegramAdminBot()
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")

if __name__ == '__main__':
    asyncio.run(main())