#!/usr/bin/env python3
import os
import sys
import time
import json
import sqlite3
import asyncio
import argparse
from datetime import datetime
from typing import List, Dict, Optional
import aiofiles
from telethon import TelegramClient, errors
from telethon.tl import types
import questionary
from pyfiglet import Figlet
from termcolor import colored
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, BarColumn
from cryptography.fernet import Fernet

# Initialize console
console = Console()

class ConfigManager:
    def __init__(self):
        self.config_file = "config.json"
        self.config = {
            "api_id": None,
            "api_hash": None,
            "session_name": "telegram_scraper",
            "log_storage": {"max_file_size": 50},
            "scraping": {"delay": 1, "retries": 3}
        }

    async def setup(self):
        if not os.path.exists(self.config_file):
            await self._first_time_setup()
        else:
            self._load_config()

    async def _first_time_setup(self):
        console.print("\n[bold yellow]First time setup[/bold yellow]")
        console.print("Get your API credentials from https://my.telegram.org/apps\n")
        
        self.config["api_id"] = await questionary.text(
            "Enter your API ID:",
            validate=lambda x: x.isdigit()
        ).ask_async()
        
        self.config["api_hash"] = await questionary.text(
            "Enter your API hash:"
        ).ask_async()
        
        self.config["session_name"] = await questionary.text(
            "Enter session name (default: telegram_scraper):",
            default="telegram_scraper"
        ).ask_async()
        
        with open(self.config_file, "w") as f:
            json.dump(self.config, f, indent=2)
        
        console.print("\n[green]✓ Configuration saved[/green]")

    def _load_config(self):
        with open(self.config_file, "r") as f:
            self.config = json.load(f)

class DatabaseManager:
    def __init__(self, db_name="telegram_logs.db"):
        self.db_name = db_name
        self.conn = None
        self._initialize_db()

    def _initialize_db(self):
        try:
            self.conn = sqlite3.connect(self.db_name)
            cursor = self.conn.cursor()
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                chat_id INTEGER,
                chat_title TEXT,
                sender_id INTEGER,
                sender_name TEXT,
                date TIMESTAMP,
                content TEXT,
                media_path TEXT,
                media_type TEXT,
                file_size INTEGER
            )
            """)
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                last_scraped TIMESTAMP
            )
            """)
            
            self.conn.commit()
        except Exception as e:
            console.print(f"[red]Database error: {e}[/red]")
            raise

class TelegramScraper:
    def __init__(self, config):
        self.config = config
        self.db = DatabaseManager()
        self.client = None
        self.encryption_key = Fernet.generate_key()
        self.cipher = Fernet(self.encryption_key)

    async def start_client(self):
        try:
            self.client = TelegramClient(
                self.config["session_name"],
                self.config["api_id"],
                self.config["api_hash"]
            )
            await self.client.start()
            return True
        except Exception as e:
            console.print(f"[red]Failed to start client: {e}[/red]")
            return False

    async def scrape_chat(self, chat_id, limit=None):
        try:
            chat = await self.client.get_entity(chat_id)
            console.print(f"\n[green]Scraping {chat.title}...[/green]")
            
            with Progress("[progress.description]{task.description}",
                        BarColumn(),
                        "[progress.percentage]{task.percentage:>3.0f}%") as progress:
                task = progress.add_task("Scraping...", total=limit)
                
                async for message in self.client.iter_messages(chat, limit=limit):
                    try:
                        await self._process_message(message)
                        progress.update(task, advance=1)
                        await asyncio.sleep(self.config["scraping"]["delay"])
                    except Exception as e:
                        console.print(f"[yellow]Skipping message: {e}[/yellow]")
            
            console.print(f"[green]Finished scraping {chat.title}[/green]")
            return True
        except Exception as e:
            console.print(f"[red]Scraping failed: {e}[/red]")
            return False

    async def _process_message(self, message):
        chat = await message.get_chat()
        sender = await message.get_sender()
        
        media_path, media_type, file_size = await self._download_media(message)
        
        content = message.text or ""
        if content:
            content = self.cipher.encrypt(content.encode()).decode()
        
        cursor = self.db.conn.cursor()
        cursor.execute("""
        INSERT INTO logs (
            message_id, chat_id, chat_title, sender_id, sender_name,
            date, content, media_path, media_type, file_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message.id,
            chat.id,
            getattr(chat, 'title', 'Private Chat'),
            sender.id if sender else None,
            getattr(sender, 'first_name', 'Unknown'),
            message.date,
            content,
            media_path,
            media_type,
            file_size
        ))
        
        cursor.execute("""
        INSERT OR REPLACE INTO chats (chat_id, title, last_scraped)
        VALUES (?, ?, ?)
        """, (chat.id, getattr(chat, 'title', 'Private Chat'), datetime.now()))
        
        self.db.conn.commit()

    async def _download_media(self, message):
        if not message.media:
            return None, None, None
        
        try:
            chat_id = (await message.get_chat()).id
            media_dir = os.path.join("telegram_data", str(chat_id), "media")
            os.makedirs(media_dir, exist_ok=True)
            
            if isinstance(message.media, types.MessageMediaPhoto):
                ext = "jpg"
                media_type = "photo"
            else:
                ext = "bin"
                media_type = "document"
            
            filename = f"{message.id}_{int(time.time())}.{ext}"
            filepath = os.path.join(media_dir, filename)
            
            await self.client.download_media(message.media, file=filepath)
            file_size = os.path.getsize(filepath)
            
            return filepath, media_type, file_size
        except Exception as e:
            console.print(f"[yellow]Media download failed: {e}[/yellow]")
            return None, None, None

    async def search_messages(self, query, chat_id=None, limit=100):
        try:
            cursor = self.db.conn.cursor()
            
            if chat_id:
                cursor.execute("""
                SELECT * FROM logs 
                WHERE chat_id = ? 
                ORDER BY date DESC 
                LIMIT ?
                """, (chat_id, limit))
            else:
                cursor.execute("""
                SELECT * FROM logs 
                ORDER BY date DESC 
                LIMIT ?
                """, (limit,))
            
            results = cursor.fetchall()
            
            table = Table(title="Search Results")
            table.add_column("Date", style="dim")
            table.add_column("Chat")
            table.add_column("Sender")
            table.add_column("Message")
            
            for row in results:
                content = self.cipher.decrypt(row[7].encode()).decode() if row[7] else ""
                
                if query.lower() in content.lower():
                    table.add_row(
                        str(row[6]),
                        row[3],
                        row[5],
                        content[:100] + "..." if len(content) > 100 else content
                    )
            
            console.print(table)
            return True
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            return False

    async def export_data(self, chat_id=None, format="json"):
        try:
            cursor = self.db.conn.cursor()
            
            if chat_id:
                cursor.execute("SELECT * FROM logs WHERE chat_id = ?", (chat_id,))
                filename = f"export_chat_{chat_id}.{format}"
            else:
                cursor.execute("SELECT * FROM logs")
                filename = f"export_all.{format}"
            
            results = cursor.fetchall()
            data = []
            
            for row in results:
                content = self.cipher.decrypt(row[7].encode()).decode() if row[7] else ""
                data.append({
                    "message_id": row[1],
                    "chat_id": row[2],
                    "chat_title": row[3],
                    "sender_id": row[4],
                    "sender_name": row[5],
                    "date": str(row[6]),
                    "content": content,
                    "media_path": row[8],
                    "media_type": row[9],
                    "file_size": row[10]
                })
            
            os.makedirs("exports", exist_ok=True)
            filepath = os.path.join("exports", filename)
            
            if format == "json":
                with open(filepath, "w") as f:
                    json.dump(data, f, indent=2)
            elif format == "csv":
                import pandas as pd
                pd.DataFrame(data).to_csv(filepath, index=False)
            
            console.print(f"[green]Data exported to {filepath}[/green]")
            return True
        except Exception as e:
            console.print(f"[red]Export failed: {e}[/red]")
            return False

class MenuSystem:
    def __init__(self, scraper):
        self.scraper = scraper
        self.running = True

    async def show_main_menu(self):
        while self.running:
            self._clear_screen()
            self._show_header()
            
            choice = await questionary.select(
                "Select an option:",
                choices=[
                    "Scrape Chat",
                    "Search Messages",
                    "Export Data",
                    "List Chats",
                    "Exit"
                ]
            ).ask_async()
            
            if choice == "Scrape Chat":
                await self.scrape_chat_menu()
            elif choice == "Search Messages":
                await self.search_menu()
            elif choice == "Export Data":
                await self.export_menu()
            elif choice == "List Chats":
                await self.list_chats()
            elif choice == "Exit":
                self.running = False

    async def scrape_chat_menu(self):
        chat_id = await questionary.text(
            "Enter Chat ID or Username:",
            validate=lambda x: len(x) > 0
        ).ask_async()
        
        limit = await questionary.text(
            "Number of messages to scrape (leave empty for all):",
            validate=lambda x: x.isdigit() or x == ""
        ).ask_async()
        
        try:
            if limit:
                await self.scraper.scrape_chat(chat_id, int(limit))
            else:
                await self.scraper.scrape_chat(chat_id)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        
        await questionary.text("Press Enter to continue...").ask_async()

    async def search_menu(self):
        query = await questionary.text("Search query:").ask_async()
        chat_id = await questionary.text(
            "Chat ID to search (leave empty for all chats):",
            default=""
        ).ask_async()
        
        try:
            if chat_id:
                await self.scraper.search_messages(query, int(chat_id))
            else:
                await self.scraper.search_messages(query)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        
        await questionary.text("Press Enter to continue...").ask_async()

    async def export_menu(self):
        chat_id = await questionary.text(
            "Chat ID to export (leave empty for all chats):",
            default=""
        ).ask_async()
        
        format = await questionary.select(
            "Export format:",
            choices=["json", "csv"]
        ).ask_async()
        
        try:
            if chat_id:
                await self.scraper.export_data(int(chat_id), format)
            else:
                await self.scraper.export_data(None, format)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        
        await questionary.text("Press Enter to continue...").ask_async()

    async def list_chats(self):
        try:
            cursor = self.scraper.db.conn.cursor()
            cursor.execute("SELECT chat_id, title FROM chats ORDER BY title")
            chats = cursor.fetchall()
            
            table = Table(title="Your Chats")
            table.add_column("ID")
            table.add_column("Title")
            
            for chat in chats:
                table.add_row(str(chat[0]), chat[1])
            
            console.print(table)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        
        await questionary.text("Press Enter to continue...").ask_async()

    def _clear_screen(self):
        os.system('cls' if os.name == 'nt' else 'clear')

    def _show_header(self):
        f = Figlet(font='slant')
        console.print(f.renderText('Telegram Scraper'), style="bold blue")
        console.print("Advanced message scraper and analyzer\n", style="dim")

async def main():
    # Check if requirements are installed
    try:
        import telethon
    except ImportError:
        console.print("[red]Error: Required packages not installed[/red]")
        console.print("Please run: pip install telethon questionary pyfiglet termcolor rich pandas cryptography aiofiles")
        return
    
    # Setup configuration
    config_manager = ConfigManager()
    await config_manager.setup()
    
    # Start scraper
    scraper = TelegramScraper(config_manager.config)
    if not await scraper.start_client():
        return
    
    # Start menu system
    menu = MenuSystem(scraper)
    await menu.show_main_menu()
    
    await scraper.client.disconnect()
    console.print("[green]Goodbye![/green]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[red]Script interrupted by user[/red]")
        sys.exit(0)