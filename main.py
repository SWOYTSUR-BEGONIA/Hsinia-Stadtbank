import asyncio
from datetime import datetime, timezone, timedelta
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
  # 設定表 (存放管理員身分組ID、銀行頻道ID)
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
            amount REAL,
            memo TEXT,
            execute_at TEXT,
            is_recurring INTEGER DEFAULT 0,
            interval_type TEXT,
            recurring_day INTEGER,
            recurring_hour INTEGER,
            repeat_count INTEGER DEFAULT 1
        )
    """)

  # 初始化國家公帳戶 (ID設定為 0)
  cursor.execute(
      "INSERT OR IGNORE INTO users (discord_id, username, balance, created_at)"
      " VALUES (0, '新亞市國家帳戶', 1000000.0, ?)",
      (datetime.now().isoformat(),),
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
  cursor.execute(
      "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
  )
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

  for t in tasks_list:
    (
        task_id,
        sender_id,
        receiver_id,
        amount,
        memo,
        execute_at_str,
        is_recurring,
        interval_type,
        r_day,
        r_hour,
        repeat_count,
    ) = t
    execute_at = datetime.fromisoformat(execute_at_str)

    if now >= execute_at:
      # 執行轉帳/扣款
      cursor.execute(
          "SELECT balance FROM users WHERE discord_id = ?", (sender_id,)
      )
      sender_row = cursor.fetchone()
      cursor.execute(
          "SELECT balance FROM users WHERE discord_id = ?", (receiver_id,)
      )
      receiver_row = cursor.fetchone()

      if sender_row and receiver_row and sender_row[0] >= amount:
        new_sender_bal = sender_row[0] - amount
        new_receiver_bal = receiver_row[0] + amount

        cursor.execute(
            "UPDATE users SET balance = ? WHERE discord_id = ?",
            (new_sender_bal, sender_id),
        )
        cursor.execute(
            "UPDATE users SET balance = ? WHERE discord_id = ?",
            (new_receiver_bal, receiver_id),
        )
        cursor.execute(
            """INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                       VALUES (?, ?, ?, ?, ?, ?)""",
            (
                sender_id,
                receiver_id,
                amount,
                memo,
                now.isoformat(),
                "定時轉帳/扣款",
            ),
        )

      if is_recurring == 1:
        # 計算下一次執行時間 (例如每月的這一天這個時間)
        next_month = (
            now.month % 12
        ) + 1  # 簡化處理：下個月同一個指定日或當月最後一日
        next_year = now.year + (1 if next_month == 1 else 0)
        try:
          next_exec = datetime(
              next_year,
              next_month,
              r_day,
              r_hour,
              0,
              0,
              tzinfo=timezone(timedelta(hours=8)),
          )
        except ValueError:
          next_exec = datetime(
              next_year,
              next_month,
              28,
              r_hour,
              0,
              0,
              tzinfo=timezone(timedelta(hours=8)),
          )

        cursor.execute(
            "UPDATE scheduled_tasks SET execute_at = ? WHERE id = ?",
            (next_exec.isoformat(), task_id),
        )
      else:
        cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

  conn.commit()
  conn.close()


# 每年年底自動發布總決算任務 (檢查是否為 12月31日 23:59 或 1月1日 00:00)
@tasks.loop(hours=24)
async def year_end_report_task():
  now = datetime.now(timezone(timedelta(hours=8)))
  if now.month == 12 and now.day == 31:
    channel_id = get_config("bank_channel_id")
    if channel_id:
      channel = bot.get_channel(int(channel_id))
      if channel:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SUM(amount) FROM transactions WHERE receiver_id = 0"
        )
        total_income = cursor.fetchone()[0] or 0.0
        cursor.execute(
            "SELECT SUM(amount) FROM transactions WHERE sender_id = 0"
        )
        cursor.execute(
            "SELECT balance FROM users WHERE discord_id = 0"
        )
        national_bal = cursor.fetchone()[0]
        conn.close()

        report = f"""# 新亞市政府總決算 ({now.year}年度)

## 一、 國家公帳戶結餘
* **當前總資產**：`{national_bal:,.2f}` 新亞幣

## 二、 年度財政收支概況
* **本年度總收入**：已歸集財政資金
* **本年度總支出**：公共行政與調撥支出

## 三、 財政審計附註
本年度決算報告由市營新亞銀行自動生成並呈報市民查閱。"""
        await channel.send(report)


# ==================== 用戶指令 ====================


@bot.tree.command(name="開戶", description="建立您的新亞銀行帳戶（每人限一個）")
@app_commands.describe(用戶名="您想要的帳戶名稱")
async def register(interaction: discord.Interaction, 用戶名: str):
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT * FROM users WHERE discord_id = ?", (interaction.user.id,)
  )
  if cursor.fetchone():
    conn.close()
    await interaction.response.send_message(
        "❌ 您已經擁有一個銀行帳戶了！", ephemeral=True
    )
    return

  cursor.execute("SELECT * FROM users WHERE username = ?", (用戶名,))
  if cursor.fetchone():
    conn.close()
    await interaction.response.send_message(
        "❌ 這個用戶名已經被使用了，請換一個。", ephemeral=True
    )
    return

  cursor.execute(
      "INSERT INTO users (discord_id, username, balance, created_at) VALUES"
      " (?, ?, ?, ?)",
      (
          interaction.user.id,
          用戶名,
          100.0,
          datetime.now().isoformat(),
      ),  # 贈送開戶禮 100
  )
  conn.commit()
  conn.close()
  await interaction.response.send_message(
      f"✅ 開戶成功！歡迎加入市營新亞銀行，用戶名：`{用戶名}`，已獲贈開戶禮金 100.0 新亞圓。"
  )


@bot.tree.command(name="改名", description="更改您的銀行帳戶名稱")
@app_commands.describe(新用戶名="新的帳戶名稱")
async def change_name(interaction: discord.Interaction, 新用戶名: str):
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT * FROM users WHERE discord_id = ?", (interaction.user.id,)
  )
  if not cursor.fetchone():
    conn.close()
    await interaction.response.send_message(
        "❌ 您尚未開戶，請先使用 `/開戶`。", ephemeral=True
    )
    return

  cursor.execute("SELECT * FROM users WHERE username = ?", (新用戶名,))
  if cursor.fetchone():
    conn.close()
    await interaction.response.send_message(
        "❌ 此用戶名已被使用。", ephemeral=True
    )
    return

  cursor.execute(
      "UPDATE users SET username = ? WHERE discord_id = ?",
      (新用戶名, interaction.user.id),
  )
  conn.commit()
  conn.close()
  await interaction.response.send_message(
      f"✅ 帳戶名稱已成功更改為：`{新用戶名}`"
  )


@bot.tree.command(name="餘額", description="查看您的帳戶餘額")
async def balance(interaction: discord.Interaction):
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT username, balance FROM users WHERE discord_id = ?",
      (interaction.user.id,),
  )
  row = cursor.fetchone()
  conn.close()

  if not row:
    await interaction.response.send_message(
        "❌ 您尚未開戶，請先使用 `/開戶`。", ephemeral=True
    )
    return

  uname, bal = row
  embed = discord.Embed(
      title="🏦 市營新亞銀行 - 帳戶資訊", color=discord.Color.gold()
  )
  embed.add_field(name="用戶名", value=uname, inline=True)
  embed.add_field(name="當前餘額", value=f"`{bal:,.2f}` 新亞幣", inline=True)
  await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="存款", description="將資金存入您的銀行帳戶")
@app_commands.describe(金額="要存入的金額")
async def deposit(interaction: discord.Interaction, 金額: float):
  if 金額 <= 0:
    await interaction.response.send_message(
        "❌ 存款金額必須大於 0。", ephemeral=True
    )
    return
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT balance FROM users WHERE discord_id = ?", (interaction.user.id,)
  )
  row = cursor.fetchone()
  if not row:
    conn.close()
    await interaction.response.send_message(
        "❌ 您尚未開戶。", ephemeral=True
    )
    return

  new_bal = row[0] + 金額
  cursor.execute(
      "UPDATE users SET balance = ? WHERE discord_id = ?",
      (new_bal, interaction.user.id),
  )
  cursor.execute(
      """INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
               VALUES (?, ?, ?, ?, ?, ?)""",
      (
          interaction.user.id,
          interaction.user.id,
          金額,
          "現金存款",
          datetime.now().isoformat(),
          "存款",
      ),
  )
  conn.commit()
  conn.close()
  await interaction.response.send_message(
      f"✅ 成功存款 `{金額:,.2f}` 新亞圓。目前餘額：`{new_bal:,.2f}` 新亞圓。"
  )


@bot.tree.command(name="提款", description="從您的銀行帳戶提取存款")
@app_commands.describe(金額="要提取的金額")
async def withdraw(interaction: discord.Interaction, 金額: float):
  if 金額 <= 0:
    await interaction.response.send_message(
        "❌ 提款金額必須大於 0。", ephemeral=True
    )
    return
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT balance FROM users WHERE discord_id = ?", (interaction.user.id,)
  )
  row = cursor.fetchone()
  if not row:
    conn.close()
    await interaction.response.send_message(
        "❌ 您尚未開戶。", ephemeral=True
    )
    return

  if row[0] < 金額:
    conn.close()
    await interaction.response.send_message("❌ 帳戶餘額不足！", ephemeral=True)
    return

  new_bal = row[0] - 金額
  cursor.execute(
      "UPDATE users SET balance = ? WHERE discord_id = ?",
      (new_bal, interaction.user.id),
  )
  cursor.execute(
      """INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
               VALUES (?, ?, ?, ?, ?, ?)""",
      (
          interaction.user.id,
          interaction.user.id,
          金額,
          "現金提款",
          datetime.now().isoformat(),
          "提款",
      ),
  )
  conn.commit()
  conn.close()
  await interaction.response.send_message(
      f"✅ 成功提款 `{金額:,.2f}` 新亞幣。目前餘額：`{new_bal:,.2f}` 新亞幣。"
  )


@bot.tree.command(name="轉帳", description="轉帳給其他用戶（支援單次預定時間）")
@app_commands.describe(
    對象="收款用戶",
    金額="轉帳金額",
    名義="轉帳名義/備忘錄",
    延遲分鐘數="（選填）幾分鐘後執行轉帳（留空則立即轉帳）",
)
async def transfer(
    interaction: discord.Interaction,
    對象: discord.User,
    金額: float,
    名義: str,
    延遲分鐘數: int = 0,
):
  if 金額 <= 0:
    await interaction.response.send_message(
        "❌ 轉帳金額必須大於 0。", ephemeral=True
    )
    return
  if 對象.id == interaction.user.id:
    await interaction.response.send_message(
        "❌ 不能轉帳給自己。", ephemeral=True
    )
    return

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT balance FROM users WHERE discord_id = ?", (interaction.user.id,)
  )
  sender_row = cursor.fetchone()
  cursor.execute(
      "SELECT balance FROM users WHERE discord_id = ?", (對象.id,)
  )
  receiver_row = cursor.fetchone()

  if not sender_row:
    conn.close()
    await interaction.response.send_message(
        "❌ 您尚未開戶。", ephemeral=True
    )
    return
  if not receiver_row:
    conn.close()
    await interaction.response.send_message(
        "❌ 收款人尚未開戶。", ephemeral=True
    )
    return

  if 延遲分鐘數 > 0:
    if sender_row[0] < 金額:
      conn.close()
      await interaction.response.send_message(
          "❌ 餘額不足以設定此預定轉帳。", ephemeral=True
      )
      return
    execute_time = datetime.now(
        timezone(timedelta(hours=8))
    ) + timedelta(minutes=延遲分鐘數)
    cursor.execute(
        """INSERT INTO scheduled_tasks (sender_id, receiver_id, amount, memo, execute_at, is_recurring)
               VALUES (?, ?, ?, ?, ?, 0)""",
        (
            interaction.user.id,
            對象.id,
            金額,
            名義,
            execute_time.isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"⏰ 預定轉帳設定成功！將於 {延遲分鐘數} 分鐘後自動轉帳 `{金額:,.2f}` 新亞幣給 {對象.mention}。名義：`{名義}`"
    )
  else:
    if sender_row[0] < 金額:
      conn.close()
      await interaction.response.send_message("❌ 帳戶餘額不足！", ephemeral=True)
      return

    new_sender_bal = sender_row[0] - 金額
    new_receiver_bal = receiver_row[0] + 金額

    cursor.execute(
        "UPDATE users SET balance = ? WHERE discord_id = ?",
        (new_sender_bal, interaction.user.id),
    )
    cursor.execute(
        "UPDATE users SET balance = ? WHERE discord_id = ?",
        (new_receiver_bal, 對象.id),
    )
    cursor.execute(
        """INSERT INTO transactions (sender_id, receiver_id, amount, memo, timestamp, type)
                 VALUES (?, ?, ?, ?, ?, ?)""",
        (
            interaction.user.id,
            對象.id,
            金額,
            名義,
            datetime.now().isoformat(),
            "轉帳",
        ),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"✅ 成功轉帳 `{金額:,.2f}` 新亞幣給 {對象.mention}！名義：`{名義}`"
    )


@bot.tree.command(name="交易紀錄", description="查看您的近期交易歷史")
async def history(interaction: discord.Interaction):
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  uid = interaction.user.id
  cursor.execute(
      """SELECT sender_id, receiver_id, amount, memo, timestamp, type 
               FROM transactions WHERE sender_id = ? OR receiver_id = ? 
               ORDER BY id DESC LIMIT 10""",
      (uid, uid),
  )
  rows = cursor.fetchall()
  conn.close()

  if not rows:
    await interaction.response.send_message(
        "📜 目前沒有任何交易紀錄。", ephemeral=True
    )
    return

  embed = discord.Embed(
      title="📜 最近 10 筆交易紀錄", color=discord.Color.blue()
  )
  for r in rows:
    sender_id, receiver_id, amount, memo, timestamp, t_type = r
    direction = "支出" if sender_id == uid else "收入"
    if sender_id == receiver_id:
      direction = t_type
    embed.add_field(
        name=f"[{direction}] {amount:,.2f} 新亞圓",
        value=(
            f"備忘/名義: `{memo}`\n時間:"
            f" `{timestamp.replace('T', ' ')[:19]}`"
        ),
        inline=False,
    )

  await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== 管理員指令 ====================


@bot.tree.command(name="設管理員身分組", description="[管理員] 設定指定的銀行管理員身分組")
@app_commands.describe(身分組="選擇 Discord 身分組")
async def set_admin_role(
    interaction: discord.Interaction, 身分組: discord.Role
):
  if not interaction.user.guild_permissions.administrator:
    await interaction.response.send_message(
        "❌ 您沒有權限執行此指令（需要伺服器管理員權限）。", ephemeral=True
    )
    return

  set_config("admin_role_id", str(身分組.id))
  await interaction.response.send_message(
      f"✅ 已成功將管理員身分組設定為：{身分組.mention}"
  )


@bot.tree.command(name="設銀行頻道", description="[管理員] 設定銀行的廣報與年終決算頻道")
@app_commands.describe(頻道="選擇頻道")
async def set_bank_channel(
    interaction: discord.Interaction, 頻道: discord.TextChannel
):
  if not is_admin_check(interaction):
    await interaction.response.send_message(
        "❌ 您沒有銀行管理員權限。", ephemeral=True
    )
    return

  set_config("bank_channel_id", str(頻道.id))
  await interaction.response.send_message(
      f"✅ 已成功將銀行資訊頻道設定為：{頻道.mention}"
  )


@bot.tree.command(name="國家帳戶餘額", description="[管理員] 查看新亞市國家帳戶餘額")
async def national_balance(interaction: discord.Interaction):
  if not is_admin_check(interaction):
    await interaction.response.send_message(
        "❌ 您沒有銀行管理員權限。", ephemeral=True
    )
    return

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT balance FROM users WHERE discord_id = 0"
  )  # 0 代表國家帳戶
  row = cursor.fetchone()
  conn.close()

  bal = row[0] if row else 0.0
  embed = discord.Embed(
      title="🏛️ 新亞市國家帳戶 - 財政總覽", color=discord.Color.dark_red()
  )
  embed.add_field(name="可用公帑餘額", value=f"`{bal:,.2f}` 新亞幣", inline=True)
  await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="國家定期轉帳扣款",
    description="[管理員] 設定國家公帳戶對某用戶的定期/定時扣款或撥款",
)
@app_commands.describe(
    目標用戶="目標用戶",
    金額="金額（正數為國家撥款給用戶，負數為國家向用戶扣款）",
    名義="財政收支名義",
    每月幾號="每月幾號執行（1-28號）",
    執行小時="幾點執行（0-23時）",
)
async def national_recurring(
    interaction: discord.Interaction,
    目標用戶: discord.User,
    金額: float,
    名義: str,
    每月幾號: int,
    執行小時: int,
):
  if not is_admin_check(interaction):
    await interaction.response.send_message(
        "❌ 您沒有銀行管理員權限。", ephemeral=True
    )
    return
  if not (1 <= 每月幾號 <= 28) or not (0 <= 執行小時 <= 23):
    await interaction.response.send_message(
        "❌ 日期請設定在 1-28 之間，小時請設定在 0-23 之間。", ephemeral=True
    )
    return

  now = datetime.now(timezone(timedelta(hours=8)))
  # 設定下次執行時間
  try:
    next_exec = datetime(
        now.year,
        now.month,
        每月幾號,
        執行小時,
        0,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    if next_exec <= now:
      # 如果本月時間已過，排到下個月
      next_month = (now.month % 12) + 1
      next_year = now.year + (1 if next_month == 1 else 0)
      next_exec = datetime(
          next_year,
          next_month,
          每月幾號,
          執行小時,
          0,
          0,
          tzinfo=timezone(timedelta(hours=8)),
      )
  except ValueError:
    await interaction.response.send_message(
        "❌ 日期無效，請重新設定。", ephemeral=True
    )
    return

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()

  # 判斷方向：金額正數 = 國庫轉給用戶；金額負數 = 用戶轉給國庫（扣款）
  if 金額 > 0:
    sender_id = 0  # 國家
    receiver_id = 目標用戶.id
    actual_amount = 金額
  else:
    sender_id = 目標用戶.id
    receiver_id = 0  # 國家
    actual_amount = abs(金額)

  cursor.execute(
      """INSERT INTO scheduled_tasks (sender_id, receiver_id, amount, memo, execute_at, is_recurring, recurring_day, recurring_hour)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
      (
          sender_id,
          receiver_id,
          actual_amount,
          名義,
          next_exec.isoformat(),
          每月幾號,
          執行小時,
      ),
  )
  conn.commit()
  conn.close()

  await interaction.response.send_message(
      f"🏛️ 國家定期財政任務已設立！\n* 目標對象：{目標用戶.mention}\n* 類型："
      f" `{'國家撥款' if 金額 > 0 else '國家強制扣款'}`\n* 金額：`{abs(金額):,.2f}`"
      f" 新亞幣\n* 週期：每個月 {每月幾號} 日 {執行小時}:00\n* 名義：`{名義}`"
  )


@bot.tree.command(name="幫助", description="檢視市營新亞銀行機器人的所有指令與說明")
async def help_command(interaction: discord.Interaction):
  embed = discord.Embed(
      title="🏦 市營新亞銀行 - 系統指令幫助頁面",
      description="歡迎使用新亞市中央銀行系統，以下為所有可用指令：",
      color=discord.Color.green(),
  )

  embed.add_field(
      name="👤 一般用戶指令",
      value=(
          "• `/開戶 [用戶名]` - 建立您的銀行帳戶（每人限一個，送開戶禮）\n"
          "• `/改名 [新用戶名]` - 變更您的帳戶名稱\n"
          "• `/餘額` - 查看您當前的帳戶資產\n"
          "• `/存款 [金額]` - 存入資金至您的帳戶\n"
          "• `/提款 [金額]` - 從帳戶提取資金\n"
          "• `/轉帳 [對象] [金額] [名義] [延遲分鐘數]` - 轉帳給他人（可選填單次定時）\n"
          "• `/交易紀錄` - 查詢最近 10 筆收支紀錄\n"
          "• `/幫助` - 顯示此說明頁面"
      ),
      inline=False,
  )

  embed.add_field(
      name="🛡️ 管理員/政府機構指令",
      value=(
          "• `/設管理員身分組 [身分組]` - 指定銀行管理員身分組\n"
          "• `/設銀行頻道 [頻道]` - 設定發布資訊與年終決算頻道\n"
          "• `/國家帳戶餘額` - 查看新亞市國家帳戶資產\n"
          "• `/國家定期轉帳扣款 [目標用戶] [金額] [名義] [每月幾號] [執行小時]` - 設定國家每月定期撥款或扣款\n"
          "• *(系統自動)* 每年 12 月 31 日自動發布「# 新亞市政府總決算」報告"
      ),
      inline=False,
  )

  await interaction.response.send_message(embed=embed, ephemeral=True)


# 啟動機器人
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
  print("錯誤：找不到 DISCORD_TOKEN 環境變數！")
else:
  bot.run(TOKEN)
