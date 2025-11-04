import os
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from sqlalchemy import Column, Integer, BigInteger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy.future import select

from aiohttp import web
import signal
import sys

# Load environment variables
load_dotenv()
_token = os.getenv("BOT_TOKEN")
if not _token:
    raise ValueError("BOT_TOKEN not found in environment variables")
TOKEN: str = _token

# Determine database URL. Support multiple environment variable names (`DATABASE_URL`, `DB_URL`, `Db_URL`).
_db_env = os.getenv("DATABASE_URL") or os.getenv("DB_URL") or os.getenv("Db_URL")
# Default to a file in the `data/` directory so the DB is created there on first run.
default_db = "sqlite+aiosqlite:///data/economy.db"
DB_URL = _db_env if _db_env else default_db

# Strip surrounding quotes if the env var was quoted in .env
if DB_URL and ((DB_URL.startswith('"') and DB_URL.endswith('"')) or (DB_URL.startswith("'") and DB_URL.endswith("'"))):
    DB_URL = DB_URL[1:-1]

# Ensure `data/` directory exists when using a sqlite file URL (skip in-memory sqlite)
if DB_URL.startswith("sqlite") and ":memory:" not in DB_URL:
    # Expect the file path after the first '///' (e.g. sqlite+aiosqlite:///data/economy.db or sqlite+aiosqlite:///C:/path)
    parts = DB_URL.split('///', 1)
    if len(parts) == 2:
        file_path = parts[1].strip().strip('"').strip("'")
        dirpath = os.path.dirname(file_path)
        if dirpath:
            try:
                os.makedirs(dirpath, exist_ok=True)
            except Exception:
                # If directory creation fails, continue and let the DB engine raise a clearer error later.
                pass

# --- Health Check Web Server ---
async def health_check(request):
    return web.Response(text="Bot is alive! ✅")

async def run_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render uses PORT environment variable, default to 10000
    port = int(os.getenv('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web server running on port {port}")

# SQLAlchemy Base
Base = declarative_base()


# --- Database Model ---
class EconUser(Base):
    __tablename__ = "econ_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet: Mapped[int] = mapped_column(Integer, default=0)
    bank: Mapped[int] = mapped_column(Integer, default=0)


# --- Database Manager ---
class EconManager:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_user(self, user_id: int) -> EconUser:
        result = await self.session.execute(select(EconUser).where(EconUser.user_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            user = EconUser(user_id=user_id, wallet=0, bank=0)
            self.session.add(user)
            await self.session.commit()
        return user

    async def add_coins(self, user_id: int, amount: int) -> EconUser:
        user = await self.get_user(user_id)
        user.wallet += amount
        await self.session.commit()
        return user

    async def transfer(self, user_id: int, amount: int, from_wallet: bool) -> bool:
        user = await self.get_user(user_id)
        if from_wallet:
            if user.wallet >= amount:
                user.wallet -= amount
                user.bank += amount
            else:
                return False
        else:
            if user.bank >= amount:
                user.bank -= amount
                user.wallet += amount
            else:
                return False
        await self.session.commit()
        return True


# --- Async Database Setup ---
# Create the engine after we've ensured any DB directories exist.
engine = create_async_engine(DB_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔗 Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")


# --- Graceful Shutdown Handler ---
shutdown_event = asyncio.Event()

def signal_handler(sig, frame):
    print(f"\n⚠️ Received signal {sig}, initiating graceful shutdown...")
    shutdown_event.set()

# Register signal handlers (works on Unix/Linux, limited on Windows)
try:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
except AttributeError:
    # Windows doesn't support SIGTERM
    signal.signal(signal.SIGINT, signal_handler)


# --- Slash Commands ---
@bot.tree.command(name="balance", description="Check your wallet and bank balance.")
async def balance(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        manager = EconManager(session)
        user = await manager.get_user(interaction.user.id)

        embed = discord.Embed(
            title=f"{interaction.user.name}'s Balance",
            description=f"💰 Wallet: **{user.wallet}**\n🏦 Bank: **{user.bank}**",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="work", description="Work to earn some coins.")
async def work(interaction: discord.Interaction):
    amount = random.randint(50, 200)
    async with AsyncSessionLocal() as session:
        manager = EconManager(session)
        await manager.add_coins(interaction.user.id, amount)

    await interaction.response.send_message(f"💼 You worked hard and earned **{amount}** coins!")


@bot.tree.command(name="deposit", description="Deposit coins into your bank.")
@app_commands.describe(amount="Amount of coins to deposit.")
async def deposit(interaction: discord.Interaction, amount: int):
    async with AsyncSessionLocal() as session:
        manager = EconManager(session)
        success = await manager.transfer(interaction.user.id, amount, from_wallet=True)

    if success:
        await interaction.response.send_message(f"🏦 You deposited **{amount}** coins into your bank.")
    else:
        await interaction.response.send_message("❌ Not enough coins in your wallet.")


@bot.tree.command(name="withdraw", description="Withdraw coins from your bank.")
@app_commands.describe(amount="Amount of coins to withdraw.")
async def withdraw(interaction: discord.Interaction, amount: int):
    async with AsyncSessionLocal() as session:
        manager = EconManager(session)
        success = await manager.transfer(interaction.user.id, amount, from_wallet=False)

    if success:
        await interaction.response.send_message(f"💰 You withdrew **{amount}** coins from your bank.")
    else:
        await interaction.response.send_message("❌ Not enough coins in your bank.")


@bot.tree.command(name="send", description="Send coins to another user.")
@app_commands.describe(user="The user to send coins to.", amount="Amount of coins to send.")
async def send(interaction: discord.Interaction, user: discord.User, amount: int):
    # Validation checks
    if amount <= 0:
        await interaction.response.send_message("❌ You must send a positive amount of coins.")
        return
    
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't send coins to yourself!")
        return
    
    if user.bot:
        await interaction.response.send_message("❌ You can't send coins to bots!")
        return

    async with AsyncSessionLocal() as session:
        manager = EconManager(session)
        
        # Get sender
        sender = await manager.get_user(interaction.user.id)
        
        # Check if sender has enough money
        if sender.wallet < amount:
            await interaction.response.send_message(f"❌ You don't have enough coins! You only have **{sender.wallet}** coins in your wallet.")
            return
        
        # Get or create recipient
        recipient = await manager.get_user(user.id)
        
        # Transfer coins
        sender.wallet -= amount
        recipient.wallet += amount
        await session.commit()

    await interaction.response.send_message(
        f"💸 You sent **{amount}** coins to {user.mention}!\n"
        f"Your new balance: **{sender.wallet}** coins"
    )


# --- Run Bot ---
async def shutdown_handler():
    """Wait for shutdown signal and close the bot"""
    await shutdown_event.wait()
    print("🛑 Shutdown signal received, closing bot...")
    await bot.close()

async def main():
    web_task = None
    shutdown_task = None
    
    try:
        await init_db()
        print("📊 Database initialized")
        
        # Start web server in background for uptime monitoring
        web_task = asyncio.create_task(run_web_server())
        
        # Start shutdown handler
        shutdown_task = asyncio.create_task(shutdown_handler())
        
        async with bot:
            await bot.start(TOKEN)
    except KeyboardInterrupt:
        print("\n⚠️ Keyboard interrupt received, shutting down gracefully...")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        raise
    finally:
        print("🔄 Cleaning up...")
        
        # Cancel web server task
        if web_task is not None and not web_task.done():
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        
        # Cancel shutdown handler task
        if shutdown_task is not None and not shutdown_task.done():
            shutdown_task.cancel()
            try:
                await shutdown_task
            except asyncio.CancelledError:
                pass
        
        # Close database connections
        await engine.dispose()
        print("✅ Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"❌ Critical error: {e}")
        exit(1)