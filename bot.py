#!/usr/bin/env python3
"""
XController Telegram Admin Bot

Features:
- Enforces that new members have a username (optional auto‑kick)
- Banned word filtering with progressive discipline (delete -> global ban)
- Global ban propagation across configured groups
- Periodic cleanup of deleted accounts (rotational)
- True Telegram forwarding (preserves 'Forwarded from' metadata) with per‑message random delay
- Support for specifying forward targets via FORWARD_GROUP_IDS (chat_id or chat_id:topic_id; topic IDs stored but not used for forwarding yet)

Environment (core): API_ID, API_HASH, BOT_TOKEN, SALT
Optional: BANNED_WORDS, ENFORCE_USERNAME, USERNAME_KICK_NOTICE, FORWARD_GROUP_IDS, FORWARD_DELAY_MIN, FORWARD_DELAY_MAX
"""

import os
import re
import logging
import asyncio
import sqlite3
import hashlib
import hmac
import time
import random
from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import (
    ChatBannedRights,
    MessageActionChatAddUser,
    MessageActionChatJoinedByLink
)
from telethon.errors import (
    ChatAdminRequiredError,
    UserAdminInvalidError,
    FloodWaitError
)
from dotenv import load_dotenv

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
    
    # Fallback to local data directory
    local_data_dir = Path("./data")
    local_data_dir.mkdir(exist_ok=True)
    return local_data_dir

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
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.time()
    
    async def wait_for_token(self):
        """Wait until a token is available"""
        while True:
            now = time.time()
            tokens_to_add = (now - self.last_refill) * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + tokens_to_add)
            self.last_refill = now
            
            if self.tokens >= 1:
                self.tokens -= 1
                break
            
            sleep_time = (1 - self.tokens) / self.refill_rate
            await asyncio.sleep(sleep_time)

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
            cursor.execute('SELECT count FROM violations WHERE user_hash = ?', (user_hash,))
            result = cursor.fetchone()
            conn.commit()
            return result[0]
    
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
    
    def get_cleanup_state(self, group_id: int) -> Tuple[Optional[datetime], int]:
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
        
        # Forward delay configuration
        forward_delay_min = float(os.getenv('FORWARD_DELAY_MIN', '0.8'))
        forward_delay_max = float(os.getenv('FORWARD_DELAY_MAX', '2.4'))
        
        # Ensure min <= max, swap if misordered
        if forward_delay_min > forward_delay_max:
            forward_delay_min, forward_delay_max = forward_delay_max, forward_delay_min
        
        self.forward_delay_min = forward_delay_min
        self.forward_delay_max = forward_delay_max
        
        # Manual forward group IDs from environment with topic parsing
        forward_group_ids_str = os.getenv('FORWARD_GROUP_IDS', '')
        self.manual_forward_groups = []
        self.group_topics = {}  # Store topic IDs for future use
        
        if forward_group_ids_str.strip():
            try:
                # Parse comma-separated list with topic support (chat_id:topic_id)
                for entry in forward_group_ids_str.split(','):
                    entry = entry.strip()
                    if not entry:
                        continue
                    
                    if ':' in entry:
                        # Format: chat_id:topic_id
                        chat_id_str, topic_id_str = entry.split(':', 1)
                        chat_id = int(chat_id_str.strip())
                        topic_id = int(topic_id_str.strip())
                        self.manual_forward_groups.append(chat_id)
                        self.group_topics[chat_id] = topic_id
                    else:
                        # Format: chat_id only
                        chat_id = int(entry)
                        self.manual_forward_groups.append(chat_id)
                
                logger.info(f"Loaded {len(self.manual_forward_groups)} forward group IDs from env (manual mode)")
                if self.group_topics:
                    logger.info(f"Topic IDs parsed: {self.group_topics}")
            except ValueError as e:
                logger.error(f"Invalid FORWARD_GROUP_IDS format: {e}. Using auto-discovery instead.")
                self.manual_forward_groups = []
                self.group_topics = {}
        
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
    
    def contains_banned_words(self, text: str) -> bool:
        """Check if text contains any banned words"""
        if not self.banned_words or not text:
            return False
        
        text_lower = text.lower()
        
        for banned_word in self.banned_words:
            # Word boundary check
            word_pattern = r'\b' + re.escape(banned_word) + r'\b'
            if re.search(word_pattern, text_lower):
                return True
            
            # Substring check for compound words or creative spelling
            if banned_word in text_lower:
                return True
        
        return False
    
    async def rate_limited_ban_user(self, chat_id: int, user_id: int):
        """Ban user with rate limiting"""
        try:
            await self.rate_limiter.wait_for_token()
            
            # Create ban rights (forever ban)
            ban_rights = ChatBannedRights(
                until_date=None,  # Forever
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
            logger.info(f"[GLOBAL BAN] Banned user {user_id} in group {chat_id}")
            
        except (ChatAdminRequiredError, UserAdminInvalidError):
            logger.warning(f"Cannot ban user {user_id} in group {chat_id} - insufficient permissions")
        except FloodWaitError as e:
            logger.warning(f"FloodWait when banning user {user_id}: waiting {e.seconds} seconds")
            await asyncio.sleep(e.seconds)
            # Single retry
            try:
                await self.client.edit_permissions(chat_id, user_id, ban_rights)
                logger.info(f"[GLOBAL BAN] Banned user {user_id} in group {chat_id} (after retry)")
            except Exception as retry_e:
                logger.error(f"Failed to ban user {user_id} after retry: {retry_e}")
        except Exception as e:
            logger.error(f"Error banning user {user_id} in group {chat_id}: {e}")
    
    async def discover_forward_groups(self):
        """Setup groups for forwarding (manual or auto-discovery)"""
        try:
            if self.manual_forward_groups:
                # Use manually configured groups
                self.forward_groups = self.manual_forward_groups.copy()
                logger.info(f"Using {len(self.forward_groups)} manually configured forward groups")
                return
            
            # Auto-discovery mode
            logger.info("Starting auto-discovery of forward groups...")
            discovered_groups = []
            
            async for dialog in self.client.iter_dialogs(limit=100):
                if dialog.is_group or dialog.is_channel:
                    # Check if we're admin
                    try:
                        permissions = await self.client.get_permissions(dialog.id, 'me')
                        if permissions.is_admin or permissions.is_creator:
                            discovered_groups.append(dialog.id)
                            if len(discovered_groups) >= 20:
                                break
                    except Exception as e:
                        logger.debug(f"Could not check permissions for {dialog.id}: {e}")
                        continue
            
            self.forward_groups = discovered_groups
            logger.info(f"Auto-discovered {len(self.forward_groups)} groups for forwarding")
            
        except Exception as e:
            logger.error(f"Error in auto-discovery (non-fatal): {e}")
            logger.info("Bot will continue without forwarding groups")
    
    async def handle_new_member(self, event):
        """Handle new member events and check username requirement"""
        if not self.enforce_username:
            return
        
        try:
            action = event.action
            user_ids = []
            
            if isinstance(action, MessageActionChatAddUser):
                user_ids = action.users
            elif isinstance(action, MessageActionChatJoinedByLink):
                user_ids = [event.from_id.user_id if hasattr(event.from_id, 'user_id') else event.from_id]
            else:
                return
            
            for user_id in user_ids:
                try:
                    # Get user entity
                    user = await self.client.get_entity(user_id)
                    
                    # Check if user has a username
                    if not hasattr(user, 'username') or not user.username:
                        logger.info(f"[USERNAME ENFORCE] User {user_id} has no username, kicking from group {event.chat_id}")
                        
                        # Send notice if configured
                        if self.username_kick_notice:
                            try:
                                await self.client.send_message(event.chat_id, self.username_kick_notice)
                            except Exception as notice_e:
                                logger.debug(f"Could not send username notice: {notice_e}")
                        
                        # Kick the user
                        await self.rate_limiter.wait_for_token()
                        await self.client.kick_participant(event.chat_id, user_id)
                        
                except Exception as user_e:
                    logger.error(f"Error handling user {user_id} in username enforcement: {user_e}")
                    
        except Exception as e:
            logger.error(f"Error in handle_new_member: {e}")
    
    async def handle_message_forwarding(self, event, user_id: int, message_text: str):
        """Handle message forwarding logic with true Telegram forwarding"""
        try:
            # Check if user can forward (24h cooldown)
            if not self.db.can_forward(user_id):
                return
            
            # Skip if message contains banned words
            if self.contains_banned_words(message_text):
                return
            
            # Forward to all available groups using true forwarding
            forwarded_count = 0
            for group_id in self.forward_groups:
                if group_id == event.chat_id:
                    continue  # Don't forward to the same group
                
                try:
                    await self.rate_limiter.wait_for_token()
                    
                    # True Telegram forwarding with preserved metadata
                    await self.client.forward_messages(
                        entity=group_id,
                        messages=event.message,
                        from_peer=event.chat_id
                    )
                    
                    forwarded_count += 1
                    
                    # Random delay per target
                    delay = random.uniform(self.forward_delay_min, self.forward_delay_max)
                    await asyncio.sleep(delay)
                    
                except FloodWaitError as e:
                    logger.warning(f"FloodWait when forwarding to group {group_id}: waiting {e.seconds} seconds")
                    await asyncio.sleep(e.seconds)
                    # Single retry
                    try:
                        await self.client.forward_messages(
                            entity=group_id,
                            messages=event.message,
                            from_peer=event.chat_id
                        )
                        forwarded_count += 1
                    except Exception as retry_e:
                        logger.error(f"Error forwarding to group {group_id} after retry: {retry_e}")
                except Exception as e:
                    logger.error(f"Error forwarding to group {group_id}: {e}")
            
            if forwarded_count > 0:
                logger.info(f"Forwarded message from user {user_id} to {forwarded_count} groups")
                self.db.update_forward_time(user_id)
        
        except Exception as e:
            logger.error(f"Error in message forwarding: {e}")
    
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
            
            message_text = event.message.text or ""
            
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
                try:
                    await event.delete()
                except Exception as del_e:
                    logger.warning(f"Could not delete message: {del_e}")
                
                # Track violations in database
                violation_count = self.db.add_violation(user_id)
                
                if violation_count >= 2:
                    # Second violation - global ban
                    logger.info(f"Globally banning user {user_id} for repeated banned word usage")
                    self.db.add_global_ban(user_id, "Multiple banned word violations")
                    
                    # Propagate ban to all forward groups
                    for group_id in self.forward_groups:
                        await self.rate_limited_ban_user(group_id, user_id)
                else:
                    logger.info(f"User {user_id} violation count: {violation_count}")
                
                return
            
            # Only forward if message has text content and is from a non-banned user
            if message_text:
                await self.handle_message_forwarding(event, user_id, message_text)
                    
        except Exception as e:
            logger.error(f"Error in handle_message: {e}")
    
    async def cleanup_deleted_accounts(self, group_id: int):
        """Clean up deleted accounts from a specific group"""
        try:
            last_cleanup, last_offset = self.db.get_cleanup_state(group_id)
            
            # Skip if cleaned recently (within 12 hours)
            if last_cleanup and datetime.now() - last_cleanup < timedelta(hours=12):
                return
            
            logger.info(f"Starting cleanup of deleted accounts in group {group_id}")
            
            deleted_count = 0
            participants_checked = 0
            
            async for participant in self.client.iter_participants(
                group_id, 
                limit=25,  # Process in small batches
                offset=last_offset
            ):
                participants_checked += 1
                
                # Check if account is deleted (no first_name usually indicates deleted account)
                if hasattr(participant, 'deleted') and participant.deleted:
                    try:
                        await self.rate_limiter.wait_for_token()
                        await self.client.kick_participant(group_id, participant.id)
                        deleted_count += 1
                        logger.info(f"Removed deleted account {participant.id} from group {group_id}")
                    except Exception as kick_e:
                        logger.warning(f"Could not remove deleted account {participant.id}: {kick_e}")
            
            # Update cleanup state
            new_offset = last_offset + participants_checked
            self.db.update_cleanup_state(group_id, new_offset)
            
            if deleted_count > 0:
                logger.info(f"Cleanup completed for group {group_id}: removed {deleted_count} deleted accounts")
            
        except Exception as e:
            logger.error(f"Error in cleanup_deleted_accounts for group {group_id}: {e}")
    
    async def start(self):
        """Start the bot and register event handlers"""
        await self.client.start(bot_token=self.bot_token)
        logger.info("Bot started successfully")
        
        # Discover forward groups
        await self.discover_forward_groups()
        
        # Register event handlers
        @self.client.on(events.NewMessage)
        async def on_new_message(event):
            await self.handle_message(event)
        
        @self.client.on(events.ChatAction)
        async def on_chat_action(event):
            await self.handle_new_member(event)
        
        # Start maintenance loop
        asyncio.create_task(self.maintenance_loop())
        
        logger.info("Bot is running and ready to handle events")
    
    async def maintenance_loop(self):
        """Background maintenance tasks"""
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                
                group_count = len(self.forward_groups)
                if group_count > 0:
                    # Rotational cleanup - clean one group per 12h cycle
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