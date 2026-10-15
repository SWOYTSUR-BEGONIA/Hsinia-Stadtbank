import asyncio
from datetime import datetime, timezone, timedelta
import calendar
import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands, tasks

# 資料庫初始化
DB_FILE = "bank.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 用戶表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            discord_id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            balance REAL DEFAULT 0.0,
            created_at TEXT
        )
    """)
    # 交易紀錄表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount REAL,
            memo TEXT,
            timestamp TEXT,
            type TEXT
        )
    """)
    # 設定表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # 定時/預定轉帳與扣款任務表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            target_role_id INTEGER DEFAULT 0,
            amount REAL,
            memo TEXT,
            execute_at TEXT,
            is_recurring INTEGER DEFAULT 0,
            recurring_day INTEGER,
            recurring_hour INTEGER
        )
    """)

    # 初始化國家公帳戶 (ID 0，僅在首次建立時寫入 10 億新亞圓，更新程式碼不會影響數據)
    cursor.execute("SELECT * FROM users WHERE discord_id = 0")
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (discord_id, username, balance, created_at) VALUES (0, '新亞市國家帳戶', 1000000000.0, ?)",
            (datetime.now().isoformat(),)
        )

    conn.commit()
    conn.close()


init_db()

# 機器人設定
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def get_config(key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_config(key, value):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def is_admin_check(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator:
        return True
    admin_role_id = get_config("admin_role_id")
    if admin_role_id:
        role = interaction.guild.get_role(int(admin_role_id))
        if role and role in interaction.user.roles:
            return True
    return False


def get_next_execution_date(year, month, target_day, target_hour):
    """計算下一個執行時間，若目標日超過該月最大天數，取該月最後一天"""
    max_days = calendar.monthrange(year, month)[1]
    actual_day = min(target_day, max_days)
    return datetime(year, month, actual_day, target_hour, 0, 0, tzinfo=timezone(timedelta(hours=8)))


@bot.event
async def on_ready():
    print(f"登入成功：{bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"已同步 {len(synced)} 個 Slash 指令")
    except Exception as e:
        print(e)
    background_tasks.start()
    year_end_report_task.start()


# 背景定時任務檢查器 (每分鐘執行一次)
@tasks.loop(minutes=1)
async def background_tasks():
    now = datetime.now(timezone(timedelta(hours=8)))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM scheduled_tasks")
    tasks_list = cursor.fetchall()

    bank_channel_id = get_config("bank_channel_id")
    bank_channel = bot.get_channel(int(bank_channel_id)) if bank_channel_id else None

    for t in tasks_list:
        task_id, sender_id, receiver_id, target_role_id, amount, memo, execute_at_str, is_recurring, r_day, r_hour = t
        execute_at = datetime.fromisoformat(execute_at_str)

        if now >= execute_at:
            # 身分組定期扣款/撥款
            if target_role_id and target_role_id > 0:
                guild = bot.guilds[0] if bot.guilds else None
                if guild:
                    role = guild.get_role(target_role_id)
                    if role:
                        count = 0
                        for member in role.members:
                            if sender_id == 0:  # 國家撥款給身分組成員
                                cursor.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount, member.id))
                                cursor.execute("UPDATE users SET balance = balance - ? WHERE discord_id = 0", (amount,))
                                cursor.execute("""INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                                                  VALUES (0, ?, ?, ?, ?, '國家定期撥款')""", (member.id, amount, memo, now.isoformat()))
                                count += 1
                            else:  # 身分組成員扣款給國家
                                cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (member.id,))
                                u_row = cursor.fetchone()
                                if u_row and u_row[0] >= amount:
                                    cursor.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (amount, member.id))
                                    cursor.execute("UPDATE users SET balance = balance + ? WHERE discord_id = 0", (amount,))
                                    cursor.execute("""INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                                                      VALUES (?, 0, ?, ?, ?, '國家定期扣款')""", (member.id, amount, memo, now.isoformat()))
                                    count += 1
                        
                        # 發送頻道通知與 Tag 身分組
                        if bank_channel:
                            action_text = "發放撥款" if sender_id == 0 else "完成扣款"
                            await bank_channel.send(
                                f"📢【新亞市財政通知】{role.mention} 的成員請注意：\n"
                                f"國家已成功對該身分組執行 `{action_text}`！\n"
                                f"* 每人金額：`{amount:,.2f}` 新亞圓\n"
                                f"* 事由名義：`{memo}`\n"
                                f"* 受影響人數：{count} 人"
                            )

            # 個別用戶執行
            else:
                cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (sender_id,))
                sender_row = cursor.fetchone()
                cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (receiver_id,))
                receiver_row = cursor.fetchone()

                if sender_row and receiver_row and sender_row[0] >= amount:
                    cursor.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (amount, sender_id))
                    cursor.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount, receiver_id))
                    cursor.execute("""INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                                      VALUES (?, ?, ?, ?, ?, '定時轉帳/扣款')""", (sender_id, receiver_id, amount, memo, now.isoformat()))
                    
                    if bank_channel and (sender_id == 0 or receiver_id == 0):
                        user_id = receiver_id if sender_id == 0 else sender_id
                        action_text = "已撥入您的帳戶" if sender_id == 0 else "已從您的帳戶扣除"
                        await bank_channel.send(
                            f"📢【新亞市財政通知】<@{user_id}>：\n"
                            f"國家財政款項 `{amount:,.2f}` 新亞圓 {action_text}。\n"
                            f"* 事由名義：`{memo}`"
                        )

            if is_recurring == 1:
                # 計算下個月執行時間 (支援 1~31 號自動調適最大天數)
                next_month = (now.month % 12) + 1
                next_year = now.year + (1 if next_month == 1 else 0)
                next_exec = get_next_execution_date(next_year, next_month, r_day, r_hour)

                cursor.execute("UPDATE scheduled_tasks SET execute_at = ? WHERE id = ?", (next_exec.isoformat(), task_id))
            else:
                cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

    conn.commit()
    conn.close()


# 每年年底自動發布總決算任務 (12月31日 23:59)
@tasks.loop(hours=12)
async def year_end_report_task():
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.month == 12 and now.day == 31:
        channel_id = get_config("bank_channel_id")
        if channel_id:
            channel = bot.get_channel(int(channel_id))
            if channel:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT SUM(amount) FROM transactions WHERE receiver_id = 0")
                total_income = cursor.fetchone()[0] or 0.0
                cursor.execute("SELECT SUM(amount) FROM transactions WHERE sender_id = 0")
                total_expense = cursor.fetchone()[0] or 0.0
                cursor.execute("SELECT balance FROM users WHERE discord_id = 0")
                national_bal = cursor.fetchone()[0]
                conn.close()

                report = f"""# 新亞市政府總決算 ({now.year}年度)

## 一、 國家公帳戶資產總覽
* **當前總資產**：`{national_bal:,.2f}` 新亞圓

## 二、 年度財政總收支概況
* **本年度財政總收入**：`{total_income:,.2f}` 新亞圓
* **本年度財政總支出**：`{total_expense:,.2f}` 新亞圓

## 三、 財政審計與說明
本年度決算報告由市營新亞銀行自動結算並呈報市民查閱。"""
                await channel.send(report)


# ==================== 一般用戶指令 ====================

@bot.tree.command(name="開戶", description="建立您的新亞銀行帳戶（每人限一個）")
@app_commands.describe(用戶名="您想要的帳戶名稱")
async def register(interaction: discord.Interaction, 用戶名: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE discord_id = ?", (interaction.user.id,))
    if cursor.fetchone():
        conn.close()
        await interaction.response.send_message("❌ 您已經擁有一個銀行帳戶了！", ephemeral=True)
        return

    cursor.execute("SELECT * FROM users WHERE username = ?", (用戶名,))
    if cursor.fetchone():
        conn.close()
        await interaction.response.send_message("❌ 這個用戶名已經被使用了，請換一個。", ephemeral=True)
        return

    cursor.execute(
        "INSERT INTO users (discord_id, username, balance, created_at) VALUES (?, ?, ?, ?)",
        (interaction.user.id, 用戶名, 100.0, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 開戶成功！歡迎加入新亞銀行，用戶名：`{用戶名}`，已獲贈開戶禮金 100.0 新亞圓。")


@bot.tree.command(name="銷戶", description="銷戶並註銷您的銀行帳戶資料")
async def delete_account(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE discord_id = ?", (interaction.user.id,))
    if not cursor.fetchone():
        conn.close()
        await interaction.response.send_message("❌ 您目前沒有任何開戶紀錄。", ephemeral=True)
        return

    cursor.execute("DELETE FROM users WHERE discord_id = ?", (interaction.user.id,))
    cursor.execute("DELETE FROM scheduled_tasks WHERE sender_id = ? OR receiver_id = ?", (interaction.user.id, interaction.user.id))
    conn.commit()
    conn.close()
    await interaction.response.send_message("⚠️ 您的市營新亞銀行帳戶已成功銷戶，感謝您的使用。", ephemeral=True)


@bot.tree.command(name="改名", description="更改您的銀行帳戶名稱")
@app_commands.describe(新用戶名="新的帳戶名稱")
async def change_name(interaction: discord.Interaction, 新用戶名: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE discord_id = ?", (interaction.user.id,))
    if not cursor.fetchone():
        conn.close()
        await interaction.response.send_message("❌ 您尚未開戶，請先使用 `/開戶`。", ephemeral=True)
        return

    cursor.execute("SELECT * FROM users WHERE username = ?", (新用戶名,))
    if cursor.fetchone():
        conn.close()
        await interaction.response.send_message("❌ 此用戶名已被使用。", ephemeral=True)
        return

    cursor.execute("UPDATE users SET username = ? WHERE discord_id = ?", (新用戶名, interaction.user.id))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 帳戶名稱已成功更改為：`{新用戶名}`")


@bot.tree.command(name="餘額", description="查看您的帳戶餘額與個人資料")
async def balance(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT username, balance FROM users WHERE discord_id = ?", (interaction.user.id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await interaction.response.send_message("❌ 您尚未開戶，請先使用 `/開戶`。", ephemeral=True)
        return

    uname, bal = row
    embed = discord.Embed(title="🏦 市營新亞銀行 - 帳戶資訊", color=discord.Color.gold())
    embed.add_field(name="帳戶名稱", value=uname, inline=True)
    embed.add_field(name="當前餘額", value=f"`{bal:,.2f}` 新亞圓", inline=True)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="存款", description="將資金存入您的銀行帳戶（大額存款需受反洗錢審查）")
@app_commands.describe(金額="要存入的金額", 名義="請詳細說明資金來源/存款名義（例如：勞務薪資、商業投資收益等）")
async def deposit(interaction: discord.Interaction, 金額: float, 名義: str):
    if 金額 <= 0:
        await interaction.response.send_message("❌ 存款金額必須大於 0。", ephemeral=True)
        return

    # ==================== 巨額財富與資金來源審查邏輯 ====================
    LARGE_AMOUNT_THRESHOLD = 100000.0  # 設定巨額審查門檻：10萬新亞圓
    SUSPICIOUS_KEYWORDS = ["錢", "無", "不知道", "123", "abc", "隨便", "測試", "test", "aaa", "bbb", "xxx"]

    clean_memo = 名義.strip()

    # 如果金額超過巨額門檻，進行嚴格審查
    if 金額 >= LARGE_AMOUNT_THRESHOLD:
        # 檢查條件 1：說明長度是否過短（少於 4 個字）
        if len(clean_memo) < 4:
            await interaction.response.send_message(
                f"🚨 **存款請求已駁回！**\n"
                f"根據《新亞市反洗錢與異常資金防範條例》，單筆存款達到或超過 `{LARGE_AMOUNT_THRESHOLD:,.2f}` 新亞圓時，"
                f"必須提供詳細且合法的資金來源說明（至少 4 個字以上）。",
                ephemeral=True
            )
            return

        # 檢查條件 2：是否包含模糊或可疑關鍵字
        if clean_memo.lower() in SUSPICIOUS_KEYWORDS:
            await interaction.response.send_message(
                f"🚨 **存款請求已駁回！**\n"
                f"您填寫的資金名義 `「{名義}」` 屬於不明或不合規說明。\n"
                f"請重新進行存款並提供明確、合法的財源證明與用途名義。",
                ephemeral=True
            )
            return
    # ==================================================================

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (interaction.user.id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message("❌ 您尚未開戶，請先使用 `/開戶`。", ephemeral=True)
        return

    new_bal = row[0] + 金額
    cursor.execute("UPDATE users SET balance = ? WHERE discord_id = ?", (new_bal, interaction.user.id))
    cursor.execute("""INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                     VALUES (?, ?, ?, ?, ?, ?)""", 
                   (interaction.user.id, interaction.user.id, 金額, clean_memo, datetime.now().isoformat(), "存款"))
    conn.commit()
    conn.close()

    # 如果是大額存款且稽核通過，顯示安全驗證提示
    audit_notice = "\n🛡️ *本筆大額交易已通過新亞市金融稽核系統驗證。*" if 金額 >= LARGE_AMOUNT_THRESHOLD else ""

    await interaction.response.send_message(
        f"✅ 成功存款 `{金額:,.2f}` 新亞圓。\n"
        f"* 資金名義：`{clean_memo}`\n"
        f"* 當前餘額：`{new_bal:,.2f}` 新亞圓{audit_notice}"
    )

@bot.tree.command(name="提款", description="從您的銀行帳戶提取資金")
@app_commands.describe(金額="要提取的金額")
async def withdraw(interaction: discord.Interaction, 金額: float):
    if 金額 <= 0:
        await interaction.response.send_message("❌ 提款金額必須大於 0。", ephemeral=True)
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (interaction.user.id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message("❌ 您尚未開戶。", ephemeral=True)
        return

    if row[0] < 金額:
        conn.close()
        await interaction.response.send_message("❌ 帳戶餘額不足！", ephemeral=True)
        return

    new_bal = row[0] - 金額
    cursor.execute("UPDATE users SET balance = ? WHERE discord_id = ?", (new_bal, interaction.user.id))
    cursor.execute("""INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                     VALUES (?, ?, ?, ?, ?, ?)""", (interaction.user.id, interaction.user.id, 金額, "現金提款", datetime.now().isoformat(), "提款"))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 成功提款 `{金額:,.2f}` 新亞圓。目前餘額：`{new_bal:,.2f}` 新亞圓。")


@bot.tree.command(name="轉帳", description="轉帳給其他用戶（支援單次預定時間）")
@app_commands.describe(對象="收款用戶", 金額="轉帳金額", 名義="轉帳名義/備忘錄", 延遲分鐘數="（選填）幾分鐘後執行轉帳")
async def transfer(interaction: discord.Interaction, 對象: discord.User, 金額: float, 名義: str, 延遲分鐘數: int = 0):
    if 金額 <= 0:
        await interaction.response.send_message("❌ 轉帳金額必須大於 0。", ephemeral=True)
        return
    if 對象.id == interaction.user.id:
        await interaction.response.send_message("❌ 不能轉帳給自己。", ephemeral=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (interaction.user.id,))
    sender_row = cursor.fetchone()
    cursor.execute("SELECT balance FROM users WHERE discord_id = ?", (對象.id,))
    receiver_row = cursor.fetchone()

    if not sender_row:
        conn.close()
        await interaction.response.send_message("❌ 您尚未開戶。", ephemeral=True)
        return
    if not receiver_row:
        conn.close()
        await interaction.response.send_message("❌ 收款人尚未開戶。", ephemeral=True)
        return

    if 延遲分鐘數 > 0:
        if sender_row[0] < 金額:
            conn.close()
            await interaction.response.send_message("❌ 餘額不足以設定此預定轉帳。", ephemeral=True)
            return
        execute_time = datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=延遲分鐘數)
        cursor.execute("""INSERT INTO scheduled_tasks (sender_id, receiver_id, amount, memo, execute_at, is_recurring)
                       VALUES (?, ?, ?, ?, ?, 0)""", (interaction.user.id, 對象.id, 金額, 名義, execute_time.isoformat()))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"⏰ 預定轉帳設定成功！將於 {延遲分鐘數} 分鐘後自動轉帳 `{金額:,.2f}` 新亞圓給 {對象.mention}。名義：`{名義}`")
    else:
        if sender_row[0] < 金額:
            conn.close()
            await interaction.response.send_message("❌ 帳戶餘額不足！", ephemeral=True)
            return

        new_sender_bal = sender_row[0] - 金額
        new_receiver_bal = receiver_row[0] + 金額

        cursor.execute("UPDATE users SET balance = ? WHERE discord_id = ?", (new_sender_bal, interaction.user.id))
        cursor.execute("UPDATE users SET balance = ? WHERE discord_id = ?", (new_receiver_bal, 對象.id))
        cursor.execute("""INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                         VALUES (?, ?, ?, ?, ?, ?)""", (interaction.user.id, 對象.id, 金額, 名義, datetime.now().isoformat(), "轉帳"))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ 成功轉帳 `{金額:,.2f}` 新亞圓給 {對象.mention}！名義：`{名義}`")


@bot.tree.command(name="交易紀錄", description="查看您的近期交易歷史")
async def history(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    uid = interaction.user.id
    cursor.execute("""SELECT sender_id, receiver_id, amount, memo, timestamp, type 
                       FROM transactions WHERE sender_id = ? OR receiver_id = ? 
                       ORDER BY id DESC LIMIT 10""", (uid, uid))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📜 目前沒有任何交易紀錄。", ephemeral=True)
        return

    embed = discord.Embed(title="📜 最近 10 筆交易紀錄", color=discord.Color.blue())
    for r in rows:
        sender_id, receiver_id, amount, memo, timestamp, t_type = r
        direction = "支出" if sender_id == uid else "收入"
        if sender_id == receiver_id:
            direction = t_type
        embed.add_field(
            name=f"[{direction}] {amount:,.2f} 新亞圓",
            value=f"備忘/名義: `{memo}`\n時間: `{timestamp.replace('T', ' ')[:19]}`",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== 管理員指令 ====================

@bot.tree.command(name="設管理員身分組", description="[管理員] 設定指定的銀行管理員身分組")
@app_commands.describe(身分組="選擇 Discord 身分組")
async def set_admin_role(interaction: discord.Interaction, 身分組: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 您沒有權限執行此指令（需要伺服器管理員權限）。", ephemeral=True)
        return

    set_config("admin_role_id", str(身分組.id))
    await interaction.response.send_message(f"✅ 已成功將管理員身分組設定為：{身分組.mention}")


@bot.tree.command(name="設銀行頻道", description="[管理員] 設定銀行的廣報與年終決算頻道")
@app_commands.describe(頻道="選擇頻道")
async def set_bank_channel(interaction: discord.Interaction, 頻道: discord.TextChannel):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ 您沒有銀行管理員權限。", ephemeral=True)
        return

    set_config("bank_channel_id", str(頻道.id))
    await interaction.response.send_message(f"✅ 已成功將銀行資訊頻道設定為：{頻道.mention}")


@bot.tree.command(name="國家帳戶餘額", description="[管理員] 查看新亞市國家帳戶餘額")
async def national_balance(interaction: discord.Interaction):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ 您沒有銀行管理員權限。", ephemeral=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE discord_id = 0")
    row = cursor.fetchone()
    conn.close()

    bal = row[0] if row else 0.0
    embed = discord.Embed(title="🏛️ 新亞市國家帳戶 - 財政總覽", color=discord.Color.dark_red())
    embed.add_field(name="可用公帑餘額", value=f"`{bal:,.2f}` 新亞圓", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="國家定期轉帳扣款", description="[管理員] 設定國家對用戶或身分組的定期撥款/扣款")
@app_commands.choices(對象類型=[
    app_commands.Choice(name="個別用戶", value="user"),
    app_commands.Choice(name="整個身分組", value="role")
])
@app_commands.describe(
    對象類型="選擇針對個別用戶或身分組",
    金額="金額（正數為國家撥款，負數為國家扣款）",
    名義="財政收支事由/備忘",
    每月幾號="每月幾號執行（1-31號）",
    執行小時="幾點執行（0-23時）",
    目標用戶="（若選個別用戶）選擇對象",
    目標身分組="（若選身分組）選擇對象身分組"
)
async def national_recurring(
    interaction: discord.Interaction,
    對象類型: str,
    金額: float,
    名義: str,
    每月幾號: int,
    執行小時: int,
    目標用戶: discord.User = None,
    目標身分組: discord.Role = None
):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ 您沒有銀行管理員權限。", ephemeral=True)
        return

    if not (1 <= 每月幾號 <= 31) or not (0 <= 執行小時 <= 23):
        await interaction.response.send_message("❌ 日期請設定在 1-31 之間，小時請設定在 0-23 之間。", ephemeral=True)
        return

    if 對象類型 == "user" and not 目標用戶:
        await interaction.response.send_message("❌ 請指定【目標用戶】。", ephemeral=True)
        return
    if 對象類型 == "role" and not 目標身分組:
        await interaction.response.send_message("❌ 請指定【目標身分組】。", ephemeral=True)
        return

    now = datetime.now(timezone(timedelta(hours=8)))
    next_exec = get_next_execution_date(now.year, now.month, 每月幾號, 執行小時)
    if next_exec <= now:
        next_month = (now.month % 12) + 1
        next_year = now.year + (1 if next_month == 1 else 0)
        next_exec = get_next_execution_date(next_year, next_month, 每月幾號, 執行小時)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    if 對象類型 == "user":
        sender_id = 0 if 金額 > 0 else 目標用戶.id
        receiver_id = 目標用戶.id if 金額 > 0 else 0
        cursor.execute("""INSERT INTO scheduled_tasks (sender_id, receiver_id, amount, memo, execute_at, is_recurring, recurring_day, recurring_hour)
                           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                       (sender_id, receiver_id, abs(金額), 名義, next_exec.isoformat(), 每月幾號, 執行小時))
        target_name = 目標用戶.mention
    else:
        sender_id = 0 if 金額 > 0 else -1
        receiver_id = -1 if 金額 > 0 else 0
        cursor.execute("""INSERT INTO scheduled_tasks (sender_id, receiver_id, target_role_id, amount, memo, execute_at, is_recurring, recurring_day, recurring_hour)
                           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                       (sender_id, receiver_id, 目標身分組.id, abs(金額), 名義, next_exec.isoformat(), 每月幾號, 執行小時))
        target_name = f"身分組 {目標身分組.mention}"

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🏛️ 國家定期財政任務已設立！\n"
        f"* 對象：{target_name}\n"
        f"* 類型：`{'國家撥款' if 金額 > 0 else '國家強制扣款'}`\n"
        f"* 金額：`{abs(金額):,.2f}` 新亞圓\n"
        f"* 週期：每個月 {每月幾號} 日 {執行小時}:00\n"
        f"* 名義：`{名義}`"
    )


@bot.tree.command(name="查看國家定期任務", description="[管理員] 查看所有運行中的國家定期撥款/扣款任務")
async def list_recurring_tasks(interaction: discord.Interaction):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ 您沒有銀行管理員權限。", ephemeral=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, sender_id, receiver_id, target_role_id, amount, memo, recurring_day, recurring_hour FROM scheduled_tasks WHERE is_recurring = 1")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📋 當前沒有任何運行中的國家定期任務。", ephemeral=True)
        return

    embed = discord.Embed(title="🏛️ 國家定期財政任務清單", color=discord.Color.blue())
    for r in rows:
        t_id, s_id, r_id, role_id, amt, memo, r_day, r_hour = r
        if role_id > 0:
            target_str = f"身分組 <@&{role_id}>"
        else:
            u_id = r_id if s_id == 0 else s_id
            target_str = f"用戶 <@{u_id}>"
        
        type_str = "國家撥款" if s_id == 0 else "國家扣款"
        embed.add_field(
            name=f"任務 ID: {t_id} | {type_str}",
            value=f"* 對象: {target_str}\n* 金額: `{amt:,.2f}` 新亞圓\n* 時間: 每月 {r_day} 號 {r_hour}:00\n* 名義: `{memo}`",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="取消國家定期任務", description="[管理員] 取消指定的國家定期撥款/扣款任務")
@app_commands.describe(任務id="要取消的任務 ID（可用 /查看國家定期任務 查詢）")
async def cancel_recurring_task(interaction: discord.Interaction, 任務id: int):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ 您沒有銀行管理員權限。", ephemeral=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM scheduled_tasks WHERE id = ? AND is_recurring = 1", (任務id,))
    if not cursor.fetchone():
        conn.close()
        await interaction.response.send_message(f"❌ 找不到 ID 為 `{任務id}` 的定期任務。", ephemeral=True)
        return

    cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (任務id,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ 已成功取消任務 ID `{任務id}`！")


@bot.tree.command(name="幫助", description="檢視市營新亞銀行機器人的所有指令與說明")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🏦 市營新亞銀行 - 系統指令幫助頁面",
        description="歡迎使用新亞市中央銀行系統，以下為所有可用指令：",
        color=discord.Color.green()
    )

    embed.add_field(
        name="👤 一般用戶指令",
        value=(
            "• `/開戶 [用戶名]` - 建立您的銀行帳戶（送開戶禮 100 新亞圓）\n"
            "• `/銷戶` - 銷戶並清除個人銀行帳戶資料\n"
            "• `/改名 [新用戶名]` - 變更您的帳戶名稱\n"
            "• `/餘額` - 查看您當前的帳戶資產與個人頭像\n"
            "• `/存款 [金額]` - 存入資金至您的帳戶\n"
            "• `/提款 [金額]` - 從帳戶提取資金\n"
            "• `/轉帳 [對象] [金額] [名義] [延遲分鐘數]` - 轉帳給他人（可選填單次預定）\n"
            "• `/交易紀錄` - 查詢最近 10 筆收支明細\n"
            "• `/幫助` - 顯示此說明頁面"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡️ 管理員/政府機構指令",
        value=(
            "• `/設管理員身分組 [身分組]` - 指定銀行管理員身分組\n"
            "• `/設銀行頻道 [頻道]` - 設定發布資訊與年終決算頻道（到帳與扣款通知亦會在此發布）\n"
            "• `/國家帳戶餘額` - 查看新亞市國家帳戶資產\n"
            "• `/國家定期轉帳扣款` - 設定國家每月對【個人或身分組】定期撥款/扣款\n"
            "• `/查看國家定期任務` - 列出所有運行中的國家定期任務與 ID\n"
            "• `/取消國家定期任務 [任務id]` - 取消特定的定期任務\n"
            "• *(系統自動)* 每年 12 月 31 日自動發布「# 新亞市政府總決算」報告"
        ),
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# 啟動機器人
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("錯誤：找不到 DISCORD_TOKEN 環境變數！")
else:
    bot.run(TOKEN)
