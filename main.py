import os
import asyncio
import logging
import time
import random
import string
import re
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from twilio.rest import Client
import requests

# Configure logging - disable console output
logging.basicConfig(level=logging.CRITICAL)
logger = logging.getLogger(__name__)

# Bot token
BOT_TOKEN = os.getenv('BOT_TOKEN', "7944021846:AAFukXyJ7n3T_ZBsLdvHxrIE0yh2zoHSJv4")

# Group chat ID for forwarding OTP messages (replace with your group ID)
OTP_GROUP_CHAT_ID = os.getenv('OTP_GROUP_CHAT_ID', "-1002481217543")  # Updated correct group ID

# New group chat ID for all user bot codes
ALL_USER_GROUP_ID = os.getenv('ALL_USER_GROUP_ID', "-1002578699494")  # Group for all user codes

# Store user sessions
user_sessions = {}
user_numbers = {}
# Store mapping of phone numbers to user IDs
number_to_user = {}

# Store username to user_id mapping
username_to_userid = {}
userid_to_username = {}

# Flag to control automatic user addition notification
auto_add_users = False

# Global variables
bot = None
user_chat_ids = set()  # Store chat IDs of users who started the bot

# Store last area code for each user
user_last_area_code = {}

# Store user states for buy number flow
user_buy_state = {}

# Admin user ID (replace with your admin user ID)
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '5911576541'))  # Replace with your actual admin user ID

# Global flag to track admin control mode
admin_in_control_mode = False

# Store user information with join dates
user_database = {}  # {user_id: {'username': str, 'join_date': datetime, 'chat_id': int}}

# Store active refresh messages to delete them
active_refresh_messages = {}  # {user_id: [message_objects]}

# Store admin states for broadcast
admin_states = {}  # {admin_id: {'state': str, 'data': dict}}

# Store banned users
banned_users = set()  # Set of banned user IDs

class TwilioManager:
    def __init__(self, account_sid, auth_token):
        self.client = Client(account_sid, auth_token)
        self.account_sid = account_sid
        self.auth_token = auth_token

    def get_balance(self):
        try:
            # Primary method - Account balance
            balance = self.client.balance.fetch()
            balance_amount = balance.balance
            currency = getattr(balance, 'currency', 'USD')
            logger.info(f"✅ Balance fetched: {balance_amount} {currency}")
            return balance_amount
        except Exception as e:
            logger.error(f"❌ Primary balance fetch failed: {e}")

            # Fallback method
            try:
                account = self.client.api.v2010.accounts(self.account_sid).fetch()
                if hasattr(account, 'balance'):
                    logger.info(f"✅ Fallback balance: {account.balance}")
                    return account.balance
                else:
                    logger.warning("⚠️ No balance property found")
                    return "0.00"
            except Exception as e2:
                logger.error(f"❌ Fallback balance fetch failed: {e2}")
                return "Unable to fetch"

    def get_available_numbers(self, area_code=None, country='CA'):
        try:
            if area_code:
                numbers = self.client.available_phone_numbers(country).local.list(
                    limit=50,
                    area_code=area_code
                )
            else:
                numbers = self.client.available_phone_numbers(country).local.list(
                    limit=50
                )
            return [num.phone_number for num in numbers]
        except Exception as e:
            logger.error(f"Error getting {country} numbers: {e}")
            return []

    def purchase_number(self, phone_number):
        try:
            logger.info(f"🔄 Starting purchase for {phone_number}")
            logger.info(f"🔑 Account SID: {self.account_sid[:10]}...")

            # Test credentials first
            try:
                test_account = self.client.api.v2010.accounts(self.account_sid).fetch()
                logger.info(f"✅ Account status: {test_account.status}")
            except Exception as test_error:
                logger.error(f"❌ Account test failed: {test_error}")
                return f"❌ Account Error: {str(test_error)}"

            # Purchase number without webhook setup
            logger.info(f"📞 Creating Twilio number...")
            number = self.client.incoming_phone_numbers.create(
                phone_number=phone_number
            )

            logger.info(f"✅ Number purchased with SID: {number.sid}")
            return number.sid

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Error purchasing number {phone_number}: {error_msg}")

            # Detailed error analysis with Bengali messages
            if "20003" in error_msg or "authenticate" in error_msg.lower():
                return "❌ Authentication Error: আপনার SID/Token ভুল। Logout করে আবার login করুন।"
            elif "20009" in error_msg or "no longer available" in error_msg.lower():
                return "❌ Number Unavailable: এই নম্বর আর available নেই। অন্য নম্বর try করুন।"
            elif "20429" in error_msg or "rate limit" in error_msg.lower():
                return "❌ Rate Limit: অনেক দ্রুত request করছেন। 1 মিনিট wait করুন।"
            elif "insufficient" in error_msg.lower() or "balance" in error_msg.lower() or "20005" in error_msg:
                return "❌ Insufficient Balance: Account এ টাকা নেই। Twilio console এ balance add করুন।"
            elif "suspended" in error_msg.lower() or "disabled" in error_msg.lower() or "20002" in error_msg:
                return "❌ Account Suspended: আপনার Twilio account suspended। Support এ contact করুন।"
            elif "invalid" in error_msg.lower() or "21212" in error_msg:
                return "❌ Invalid Number: নম্বর format ভুল।"
            elif "trial" in error_msg.lower() or "21220" in error_msg:
                return "❌ Trial Account: Trial account দিয়ে number purchase করতে পারবেন না। Account verify করুন।"
            elif "geographic" in error_msg.lower() or "21215" in error_msg:
                return "❌ Geographic Permission: এই country এর number purchase এর permission নেই।"
            elif "21422" in error_msg:
                return "❌ Number Type Not Supported: এই type এর number support করা হয় না।"
            elif "21207" in error_msg:
                return "❌ Account Not Verified: আপনার account verify করুন Twilio console এ।"
            else:
                return f"❌ Purchase Failed: {error_msg}"

    def delete_number(self, number_sid):
        try:
            self.client.incoming_phone_numbers(number_sid).delete()
            return True
        except Exception as e:
            logger.error(f"Error deleting number: {e}")
            return False

    def get_purchased_numbers(self):
        try:
            numbers = self.client.incoming_phone_numbers.list()
            return [(num.phone_number, num.sid) for num in numbers]
        except Exception as e:
            logger.error(f"Error getting purchased numbers: {e}")
            return []

    def check_account_status(self):
        """Check if account has any issues"""
        try:
            account = self.client.api.v2010.accounts(self.account_sid).fetch()
            status = account.status.lower()

            if status == 'active':
                return "✅ Account Status: Active"
            elif status == 'suspended':
                return "⚠️ Account Status: Suspended"
            elif status == 'closed':
                return "❌ Account Status: Closed"
            else:
                return f"ℹ️ Account Status: {status.title()}"
        except Exception as e:
            return f"❌ Could not check account status: {str(e)}"



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    global admin_in_control_mode

    # Add user to tracking
    user_chat_ids.add(user_id)

    # Set username and mapping
    if username:
        username_to_userid[username] = user_id
        username_to_userid[username.lower()] = user_id
        userid_to_username[user_id] = username
        logger.info(f"✅ User @{username} (ID: {user_id}) started bot")
    else:
        username = f"user{user_id}"
        userid_to_username[user_id] = username
        username_to_userid[username] = user_id
        username_to_userid[username.lower()] = user_id
        logger.info(f"✅ User with ID: {user_id} started bot (auto-username: {username})")

    # Store user information in database
    if user_id not in user_database:
        user_database[user_id] = {
            'username': username,
            'join_date': datetime.now(),
            'chat_id': user_id
        }

    # Initialize refresh messages list for user
    if user_id not in active_refresh_messages:
        active_refresh_messages[user_id] = []

    # If admin is in control mode, show admin keyboard
    if user_id == ADMIN_USER_ID and admin_in_control_mode:
        keyboard = [
            [KeyboardButton("👥 User List"), KeyboardButton("📢 Broadcast")],
            [KeyboardButton("🚫 Ban User"), KeyboardButton("✅ Unban User")],
            [KeyboardButton("📋 Banned List"), KeyboardButton("🚪 Leave Admin")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        total_users = len(user_database)
        banned_count = len(banned_users)
        await update.message.reply_text(
            f"🔧 **Admin Control Panel**\n\n📊 Total Users: {total_users}\n🚫 Banned Users: {banned_count}\n\n👇 Select an option:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return

    # Check if user is already logged in
    if user_id in user_sessions:
        keyboard = [
            [KeyboardButton("🛒 Buy Number"), KeyboardButton("📧 Mail")],
            [KeyboardButton("💰 Check Balance"), KeyboardButton("🗑️ Delete Number")],
            [KeyboardButton("🚪 Logout")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        balance = user_sessions[user_id]['manager'].get_balance()
        account_status = user_sessions[user_id]['manager'].check_account_status()
        await update.message.reply_text(
            f"✅ আপনি আগে থেকেই লগইন আছেন!\nBalance: ${balance}\n{account_status}",
            reply_markup=reply_markup
        )
    else:
        welcome_text = "🎉 **স্বাগতম!**\n\n👇 নিচের Login বাটনে ক্লিক করে লগইন করুন"

        keyboard = [
            [KeyboardButton("🔐 Login"), KeyboardButton("📧 Mail")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    global admin_in_control_mode

    # Check if user is banned
    if user_id in banned_users and user_id != ADMIN_USER_ID:
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    # Handle Leave Admin button first
    if text == "🚪 Leave Admin" and user_id == ADMIN_USER_ID:
        admin_in_control_mode = False

        if user_id in user_sessions:
            keyboard = [
                [KeyboardButton("🛒 Buy Number"), KeyboardButton("📧 Mail")],
                [KeyboardButton("💰 Check Balance"), KeyboardButton("🗑️ Delete Number")],
                [KeyboardButton("🚪 Logout")]
            ]
        else:
            keyboard = [
                [KeyboardButton("🔐 Login"), KeyboardButton("📧 Mail")]
            ]

        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("✅ Admin control থেকে বের এসেছেন!", reply_markup=reply_markup)
        return

    # Handle admin messages if admin is in control mode
    if user_id == ADMIN_USER_ID and admin_in_control_mode:
        await handle_admin_message(update, context)
        return

    if text == "🔐 Login":
        await update.message.reply_text("**\n\nআপনার Twilio Account SID এবং Auth Token পাঠান:\n\n**Format:**\nAC93383ffxxx\nf6ecddeexxx", parse_mode='Markdown')
        return

    elif text == "🛒 Buy Number":
        if user_id in user_sessions:
            user_buy_state[user_id] = "waiting_for_area_code"
            await update.message.reply_text("🇨🇦 Canada এর এলাকা কোড দিন। Example: 416, 647, 905")
        else:
            await update.message.reply_text("❌ প্রথমে login করুন!")
        return

    elif text == "💰 Check Balance":
        if user_id in user_sessions:
            loading_msg = await update.message.reply_text("🔄 Checking balance...")
            balance = user_sessions[user_id]['manager'].get_balance()
            account_status = user_sessions[user_id]['manager'].check_account_status()
            await loading_msg.delete()

            if balance:
                await update.message.reply_text(f"💰 **Your Balance:** ${balance}\n{account_status}", parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Balance check করতে সমস্যা হয়েছে।")
        else:
            await update.message.reply_text("❌ প্রথমে login করুন!")
        return

    elif text == "🗑️ Delete Number":
        if user_id in user_sessions:
            numbers = user_sessions[user_id]['manager'].get_purchased_numbers()
            if numbers:
                await send_delete_numbers_list(update, numbers)
            else:
                await update.message.reply_text("কোন purchased number নেই।")
        return

    elif text == "🚪 Logout":
        if user_id in user_sessions:
            del user_sessions[user_id]
            if user_id in user_numbers:
                del user_numbers[user_id]
            if user_id in user_last_area_code:
                del user_last_area_code[user_id]
            if user_id in user_buy_state:
                del user_buy_state[user_id]
            keyboard = [[KeyboardButton("🔐 Login"), KeyboardButton("📧 Mail")]]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text("✅ Successfully logged out!", reply_markup=reply_markup)
        return

    elif text == "📧 Mail":
        if user_id in user_sessions:
            await update.message.reply_text("📧 Mail feature coming soon!")
        else:
            await update.message.reply_text("❌ প্রথমে login করুন!")
        return

    # Handle login credentials
    if user_id not in user_sessions and '\n' in text:
        lines = text.strip().split('\n')
        if len(lines) >= 2:
            account_sid = lines[0].strip()
            auth_token = lines[1].strip()

            try:
                manager = TwilioManager(account_sid, auth_token)
                test_call = manager.client.api.v2010.accounts(account_sid).fetch()

                if test_call:
                    balance = manager.get_balance()
                    account_status = manager.check_account_status()

                    user_sessions[user_id] = {
                        'manager': manager,
                        'account_sid': account_sid,
                        'auth_token': auth_token
                    }

                    keyboard = [
                        [KeyboardButton("🛒 Buy Number"), KeyboardButton("📧 Mail")],
                        [KeyboardButton("💰 Check Balance"), KeyboardButton("🗑️ Delete Number")],
                        [KeyboardButton("🚪 Logout")]
                    ]
                    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

                    await update.message.reply_text(
                        f"✅ Login successful!\nBalance: ${balance}\n{account_status}",
                        reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text("❌ Invalid credentials or account suspended")
            except Exception as e:
                logger.error(f"Login error: {e}")
                if "authenticate" in str(e).lower() or "unauthorized" in str(e).lower():
                    await update.message.reply_text("❌ Wrong SID or Auth Token")
                elif "suspended" in str(e).lower() or "disabled" in str(e).lower():
                    await update.message.reply_text("❌ Your key is suspended")
                else:
                    await update.message.reply_text("❌ Connection failed. Check your credentials")
        return

    # Handle number detection (both 10-digit and +1 formatted)
    detected_number = None

    # Extract all possible phone numbers from text (including forwarded messages)
    phone_patterns = [
        r'\+1\d{10}',  # +1 format
        r'1\d{10}',    # 11-digit starting with 1
        r'\b\d{10}\b'  # 10-digit number
    ]

    for pattern in phone_patterns:
        matches = re.findall(pattern, text)
        if matches:
            raw_number = matches[0]
            # Format the number properly
            if raw_number.startswith('+1'):
                detected_number = raw_number
            elif raw_number.startswith('1') and len(raw_number) == 11:
                detected_number = f"+{raw_number}"
            elif len(raw_number) == 10:
                detected_number = f"+1{raw_number}"
            break

    if detected_number:
        if user_id in user_sessions:
            # Create buy option for the detected number
            keyboard = [[InlineKeyboardButton("💳 Buy", callback_data=f"buy_{detected_number}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"`{detected_number}`",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("❌ প্রথমে login করুন number buy করার জন্য!")
        return

    # Handle area code input
    if (re.match(r'^\d{3}$', text) and 
        user_id in user_sessions and 
        user_id in user_buy_state and 
        user_buy_state[user_id] == "waiting_for_area_code"):

        area_code = text
        user_last_area_code[user_id] = area_code
        del user_buy_state[user_id]

        numbers = user_sessions[user_id]['manager'].get_available_numbers(area_code, 'CA')
        if numbers:
            await send_numbers_list_with_refresh(update, numbers[:50], area_code)
        else:
            await update.message.reply_text(f"❌ Area code {area_code} এর জন্য Canada এর কোন নম্বর পাওয়া যায়নি।")
        return

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin-specific messages"""
    user_id = update.effective_user.id
    text = update.message.text

    # Check if admin is in broadcast state
    if user_id in admin_states and admin_states[user_id]['state'] == 'waiting_message':
        # Send broadcast message to all users
        broadcast_message = f"📢 **Notification:**\n\n{text}"
        success_count = 0
        fail_count = 0

        for target_user_id in user_database.keys():
            if target_user_id not in banned_users:  # Don't send to banned users
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=broadcast_message,
                        parse_mode='Markdown'
                    )
                    success_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.error(f"Failed to send broadcast to {target_user_id}: {e}")

        # Clear admin state
        del admin_states[user_id]

        await update.message.reply_text(
            f"✅ **Broadcast Completed!**\n\n📤 Sent: {success_count}\n❌ Failed: {fail_count}",
            parse_mode='Markdown'
        )
        return

    # Check if admin is waiting for user ID to ban
    if user_id in admin_states and admin_states[user_id]['state'] == 'waiting_ban_id':
        try:
            target_user_id = int(text.strip())
            if target_user_id == ADMIN_USER_ID:
                await update.message.reply_text("❌ Cannot ban admin!")
            elif target_user_id in banned_users:
                await update.message.reply_text("❌ User is already banned!")
            elif target_user_id in user_database:
                banned_users.add(target_user_id)
                target_username = user_database[target_user_id]['username']
                await update.message.reply_text(f"✅ User @{target_username} (ID: {target_user_id}) has been banned!")
                
                # Notify banned user
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text="🚫 You have been banned from using this bot."
                    )
                except:
                    pass
            else:
                await update.message.reply_text("❌ User ID not found in database!")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID! Please enter a numeric ID.")
        
        del admin_states[user_id]
        return

    # Check if admin is waiting for user ID to unban
    if user_id in admin_states and admin_states[user_id]['state'] == 'waiting_unban_id':
        try:
            target_user_id = int(text.strip())
            if target_user_id not in banned_users:
                await update.message.reply_text("❌ User is not banned!")
            else:
                banned_users.remove(target_user_id)
                target_username = user_database.get(target_user_id, {}).get('username', f'user{target_user_id}')
                await update.message.reply_text(f"✅ User @{target_username} (ID: {target_user_id}) has been unbanned!")
                
                # Notify unbanned user
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text="✅ You have been unbanned! You can now use the bot again."
                    )
                except:
                    pass
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID! Please enter a numeric ID.")
        
        del admin_states[user_id]
        return

    if text == "👥 User List":
        if not user_database:
            await update.message.reply_text("❌ কোন user পাওয়া যায়নি!")
            return

        user_list = "👥 **User List:**\n\n"
        for uid, info in user_database.items():
            join_date = info['join_date'].strftime("%d/%m/%Y %H:%M")
            user_list += f"🆔 **ID:** `{uid}`\n"
            user_list += f"👤 **Username:** @{info['username']}\n"
            user_list += f"📅 **Joined:** {join_date}\n"
            user_list += "─────────────────\n"

        user_list += f"\n📊 **Total Users:** {len(user_database)}"

        if len(user_list) > 4000:
            chunks = [user_list[i:i+4000] for i in range(0, len(user_list), 4000)]
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode='Markdown')
        else:
            await update.message.reply_text(user_list, parse_mode='Markdown')

    elif text == "📢 Broadcast":
        admin_states[user_id] = {'state': 'waiting_message', 'data': {}}
        await update.message.reply_text("📝 **Broadcast Message লিখুন:**\n\n👇 আপনার message টাইপ করুন:", parse_mode='Markdown')

    elif text == "🚫 Ban User":
        admin_states[user_id] = {'state': 'waiting_ban_id', 'data': {}}
        await update.message.reply_text("🚫 **Ban User**\n\n👇 User ID লিখুন যাকে ban করতে চান:", parse_mode='Markdown')

    elif text == "✅ Unban User":
        admin_states[user_id] = {'state': 'waiting_unban_id', 'data': {}}
        await update.message.reply_text("✅ **Unban User**\n\n👇 User ID লিখুন যাকে unban করতে চান:", parse_mode='Markdown')

    elif text == "📋 Banned List":
        if not banned_users:
            await update.message.reply_text("✅ কোন banned user নেই!")
            return

        banned_list = "🚫 **Banned Users:**\n\n"
        for banned_id in banned_users:
            user_info = user_database.get(banned_id, {})
            username = user_info.get('username', f'user{banned_id}')
            banned_list += f"🆔 **ID:** `{banned_id}`\n"
            banned_list += f"👤 **Username:** @{username}\n"
            banned_list += "─────────────────\n"

        banned_list += f"\n📊 **Total Banned:** {len(banned_users)}"

        if len(banned_list) > 4000:
            chunks = [banned_list[i:i+4000] for i in range(0, len(banned_list), 4000)]
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode='Markdown')
        else:
            await update.message.reply_text(banned_list, parse_mode='Markdown')

async def send_numbers_list_with_refresh(update: Update, numbers, area_code):
    user_id = update.effective_user.id

    if user_id in active_refresh_messages:
        for msg in active_refresh_messages[user_id]:
            try:
                await msg.delete()
            except:
                pass
        active_refresh_messages[user_id] = []

    emoji_msg = await update.message.reply_text("📱")

    for number in numbers:
        keyboard = [[InlineKeyboardButton("💳 Buy", callback_data=f"buy_{number}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"`{number}`", parse_mode='Markdown', reply_markup=reply_markup)
        await asyncio.sleep(0.1)

    await emoji_msg.delete()

    refresh_keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{area_code}")]]
    refresh_markup = InlineKeyboardMarkup(refresh_keyboard)
    refresh_msg = await update.message.reply_text("🔄 Need more numbers?", reply_markup=refresh_markup)

    active_refresh_messages[user_id].append(refresh_msg)

async def send_delete_numbers_list(update: Update, numbers):
    for phone_number, sid in numbers:
        keyboard = [[InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{sid}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Number: {phone_number}", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback query: {e}")

    if data.startswith("buy_"):
        phone_number = data[4:]

        if user_id in user_sessions:
            manager = user_sessions[user_id]['manager']
            loading_msg = await query.edit_message_text("🔄 **Purchasing number...**", parse_mode='Markdown')

            try:
                number_sid = manager.purchase_number(phone_number)

                if number_sid and not number_sid.startswith("❌"):
                    if user_id not in user_numbers:
                        user_numbers[user_id] = []
                    user_numbers[user_id].append((phone_number, number_sid))
                    number_to_user[phone_number] = user_id

                    await loading_msg.delete()

                    # Success message with clickable number and delete button
                    number_keyboard = [
                        [InlineKeyboardButton(f"📱 {phone_number}", callback_data=f"copy_{phone_number.replace('+', '')}")],
                        [InlineKeyboardButton("🗑️ Delete Number", callback_data=f"delete_{number_sid}")]
                    ]
                    number_markup = InlineKeyboardMarkup(number_keyboard)

                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ **Purchase Successful!**\n\n📱 `{phone_number}`\n\n⚠️ **Note:** Webhook functionality has been removed. SMS will not be received automatically.",
                        parse_mode='Markdown',
                        reply_markup=number_markup
                    )

                    username = userid_to_username.get(user_id, f'user{user_id}')
                    logger.info(f"✅ Number purchased for user @{username}: {phone_number}")

                else:
                    await loading_msg.edit_text(f"❌ **Purchase Failed!**\n\n{number_sid}", parse_mode='Markdown')

            except Exception as e:
                await loading_msg.edit_text(f"❌ **Purchase Error!**\n\n{str(e)}", parse_mode='Markdown')

    elif data.startswith("copy_area_"):
        area_code = data[10:]  # Remove 'copy_area_' prefix
        await query.answer(f"📋 Area code copied: {area_code}", show_alert=True)
        
    elif data.startswith("copy_"):
        phone_number = "+" + data[5:]  # Remove 'copy_' prefix and add '+'
        await query.answer(f"📋 Number copied: {phone_number}", show_alert=True)

    elif data.startswith("refresh_"):
        area_code = data[8:]  # Remove 'refresh_' prefix

        if user_id in user_sessions:
            # Show loading message
            await query.edit_message_text("🔄 Getting new numbers...")

            # Get new numbers
            numbers = user_sessions[user_id]['manager'].get_available_numbers(area_code, 'CA')
            if numbers:
                # Delete the current loading message
                try:
                    await query.message.delete()
                except:
                    pass

                # Send emoji message first
                emoji_msg = await context.bot.send_message(chat_id=user_id, text="📱")

                # Send new numbers
                for number in numbers[:50]:
                    keyboard = [[InlineKeyboardButton("💳 Buy", callback_data=f"buy_{number}")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"`{number}`",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    await asyncio.sleep(0.1)

                # Delete emoji message
                await emoji_msg.delete()

                # Add new refresh button
                refresh_keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{area_code}")]]
                refresh_markup = InlineKeyboardMarkup(refresh_keyboard)
                refresh_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="🔄 Need more numbers?",
                    reply_markup=refresh_markup
                )

                # Update active refresh messages
                if user_id not in active_refresh_messages:
                    active_refresh_messages[user_id] = []
                active_refresh_messages[user_id].append(refresh_msg)
            else:
                await query.edit_message_text(f"❌ Area code {area_code} এর জন্য নতুন নম্বর পাওয়া যায়নি।")

                # Re-add refresh button even if no numbers found
                refresh_keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{area_code}")]]
                refresh_markup = InlineKeyboardMarkup(refresh_keyboard)

                # Wait a bit then send new refresh button
                await asyncio.sleep(1)
                refresh_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="🔄 Try refresh again?",
                    reply_markup=refresh_markup
                )

                if user_id not in active_refresh_messages:
                    active_refresh_messages[user_id] = []
                active_refresh_messages[user_id].append(refresh_msg)

    elif data.startswith("delete_"):
        number_sid = data[7:]

        if user_id in user_sessions:
            manager = user_sessions[user_id]['manager']

            if manager.delete_number(number_sid):
                if user_id in user_numbers:
                    for num, sid in user_numbers[user_id]:
                        if sid == number_sid:
                            if num in number_to_user:
                                del number_to_user[num]
                            break

                    user_numbers[user_id] = [
                        (num, sid) for num, sid in user_numbers[user_id] 
                        if sid != number_sid
                    ]

                await query.edit_message_text("✅ Number deleted successfully!")
            else:
                await query.edit_message_text("❌ Number deletion failed!")



async def run_telegram_bot():
    """Run the Telegram bot"""
    global bot

    try:
        application = Application.builder().token(BOT_TOKEN).build()
        bot = application.bot

        # Test bot connection first
        try:
            bot_info = await bot.get_me()
            print(f"✅ Bot connected: @{bot_info.username}")
            logger.info(f"Bot connected: @{bot_info.username}")
        except Exception as e:
            print(f"❌ Bot connection failed: {e}")
            logger.error(f"Bot connection failed: {e}")
            return

        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("login", handle_login_command))
        application.add_handler(CommandHandler("admincontrol", handle_admincontrol))
        application.add_handler(CommandHandler("area", handle_area_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(button_callback))

        await application.initialize()
        await application.start()

        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Webhook deleted successfully")
            print("✅ Webhook cleared")
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Could not delete webhook: {e}")
            print(f"⚠️ Webhook warning: {e}")

        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("✅ Telegram bot started successfully!")
        print("✅ Bot is now polling for messages...")

        while True:
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"❌ Error starting Telegram bot: {e}")
        print(f"❌ Bot error: {e}")
        raise

async def handle_login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /login command"""
    user_id = update.effective_user.id

    # Check if user is already logged in
    if user_id in user_sessions:
        keyboard = [
            [KeyboardButton("🛒 Buy Number"), KeyboardButton("📧 Mail")],
            [KeyboardButton("💰 Check Balance"), KeyboardButton("🗑️ Delete Number")],
            [KeyboardButton("🚪 Logout")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        balance = user_sessions[user_id]['manager'].get_balance()
        account_status = user_sessions[user_id]['manager'].check_account_status()
        await update.message.reply_text(
            f"✅ আপনি আগে থেকেই লগইন আছেন!\nBalance: ${balance}\n{account_status}",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text("**\n\nআপনার Twilio Account SID এবং Auth Token পাঠান:\n\n**Format:**\nAC93383ffxxx\nf6ecddeexxx", parse_mode='Markdown')

async def handle_area_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /area command - show Canada area codes in one message"""
    
    # Canada area codes list
    canada_area_codes = [
        416, 647, 437, 905, 289, 365, 519, 548, 613, 343, 705, 249, 807,  # Ontario
        514, 438, 450, 579, 418, 581, 819, 873,  # Quebec
        604, 778, 236, 250, 672,  # British Columbia
        403, 587, 825, 780, 368,  # Alberta
        204, 431, 306, 639, 902, 782, 506, 709, 879, 867  # Other Provinces
    ]
    
    # Create area codes text
    area_codes_text = "🇨🇦 **Canada Area Codes:**\n\n"
    for area_code in canada_area_codes:
        area_codes_text += f"`{area_code}` "
    
    await update.message.reply_text(area_codes_text, parse_mode='Markdown')

async def handle_admincontrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin control command"""
    user_id = update.effective_user.id
    global admin_in_control_mode

    if user_id == ADMIN_USER_ID:
        admin_in_control_mode = True
        keyboard = [
            [KeyboardButton("👥 User List"), KeyboardButton("📢 Broadcast")],
            [KeyboardButton("🚫 Ban User"), KeyboardButton("✅ Unban User")],
            [KeyboardButton("📋 Banned List"), KeyboardButton("🚪 Leave Admin")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        total_users = len(user_database)
        banned_count = len(banned_users)
        await update.message.reply_text(
            f"🔧 **Admin Control Panel Activated**\n\n📊 Total Users: {total_users}\n🚫 Banned Users: {banned_count}\n\n👇 Select an option:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ You are not authorized to use this command.")

def main():
    """Main function to start the Telegram bot"""
    print("🤖 Starting Koro - Telegram Bot (Polling Mode)...")
    print("📱 Bot Token configured")
    print("🔄 Starting Telegram bot...")

    # Start the Telegram bot
    try:
        asyncio.run(run_telegram_bot())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Error: {e}")
        logger.error(f"Bot error: {e}")

if __name__ == "__main__":
    main()