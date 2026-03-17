import discord
from discord.ext import commands
import asyncio
import logging
import os
import json
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ============================================================
# ⚙️ CẤU HÌNH - Chỉ cần chỉnh sửa phần này
# ============================================================
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

SERVERS = [
    {
        'voice_channels': [1483271561036435660],
        'report_channel': 1483288436369653861,
    },
    {
        'voice_channels': [1483284081872601098],
        'report_channel': 1483284081872601093,
    },
]

WARN_BEFORE_KICK  = 10    # Cảnh báo trước X giây
WAIT_SECONDS      = 60    # Thời gian chờ trước khi kick
REPORT_HOUR       = 23    # Giờ gửi báo cáo (24h)
REPORT_MINUTE     = 0     # Phút gửi báo cáo
DATA_FILE         = 'study_data.json'

MOTIVATIONS = [
    "💪 Hôm nay cố lên! Mỗi phút học là một bước tiến!",
    "🔥 Chăm chỉ hôm nay, thành công ngày mai!",
    "📚 Kiến thức là sức mạnh, hãy tích lũy từng ngày!",
    "⭐ Bạn đang làm rất tốt! Tiếp tục phát huy nhé!",
    "🎯 Tập trung! Mục tiêu của bạn đang chờ phía trước!",
    "🚀 Mỗi giờ học hôm nay là đầu tư cho tương lai!",
    "🌟 Không có thành công nào mà không có nỗ lực!",
    "💡 Hãy học như hôm nay là ngày cuối cùng bạn được học!",
]

# ============================================================
# 🔧 KHỞI TẠO
# ============================================================
if not TOKEN:
    raise ValueError('Không tìm thấy DISCORD_TOKEN trong file .env!')

FOCUS_CHANNEL_IDS = [ch for s in SERVERS for ch in s['voice_channels']]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True  # ✅ Thêm dòng này
bot = commands.Bot(command_prefix='!', intents=intents)

pending_checks: dict[int, asyncio.Task] = {}
join_times: dict[int, datetime] = {}

# ============================================================
# 💾 XỬ LÝ DỮ LIỆU
# ============================================================
def load_data() -> dict:
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.error(f'Lỗi đọc file dữ liệu: {e}')
    return {}

def save_data(data: dict):
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f'Lỗi lưu file dữ liệu: {e}')

def add_study_time(member_id: int, member_name: str, seconds: int):
    if seconds <= 0:
        return
    data = load_data()
    today = datetime.now().strftime('%Y-%m-%d')
    uid = str(member_id)
    if uid not in data:
        data[uid] = {'name': member_name, 'daily': {}, 'total': 0}
    data[uid]['name'] = member_name
    data[uid]['daily'][today] = data[uid]['daily'].get(today, 0) + seconds
    data[uid]['total'] = data[uid].get('total', 0) + seconds
    save_data(data)
    log.info(f'Đã lưu {format_time(seconds)} học tập cho {member_name}')

def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def get_report_channel_id(voice_channel_id: int) -> int | None:
    for server in SERVERS:
        if voice_channel_id in server['voice_channels']:
            return server['report_channel']
    return None

# ============================================================
# 🛠️ HÀM TIỆN ÍCH
# ============================================================
async def safe_send_dm(member: discord.Member, message: str):
    try:
        await member.send(message)
    except discord.Forbidden:
        log.warning(f'Không thể gửi DM cho {member.display_name} (chặn DM)')
    except discord.HTTPException as e:
        log.error(f'Lỗi HTTP gửi DM cho {member.display_name}: {e}')

def bot_can_move(member: discord.Member) -> bool:
    if not member.guild.me.guild_permissions.move_members:
        log.error(f'Bot thiếu quyền Move Members trong {member.guild.name}!')
        return False
    return True

def record_join(member: discord.Member):
    join_times[member.id] = datetime.now()
    log.info(f'{member.display_name} bắt đầu học lúc {join_times[member.id].strftime("%H:%M:%S")}')

def record_leave(member: discord.Member):
    if member.id in join_times:
        duration = int((datetime.now() - join_times.pop(member.id)).total_seconds())
        add_study_time(member.id, member.display_name, duration)
        return duration
    return 0

def cancel_task(member_id: int):
    task = pending_checks.pop(member_id, None)
    if task and not task.done():
        task.cancel()

def start_check(member: discord.Member, reason: str):
    cancel_task(member.id)
    task = asyncio.create_task(check_stream(member))
    pending_checks[member.id] = task
    log.info(f'{member.display_name} {reason} → đếm ngược {WAIT_SECONDS}s.')

# ============================================================
# 🎯 KIỂM TRA STREAM
# ============================================================
async def check_stream(member: discord.Member):
    try:
        await asyncio.sleep(WAIT_SECONDS - WARN_BEFORE_KICK)

        # Kiểm tra còn trong phòng không
        if not (member.voice
                and member.voice.channel
                and member.voice.channel.id in FOCUS_CHANNEL_IDS):
            return

        # Đã stream → bỏ qua
        if member.voice.self_stream:
            log.info(f'{member.display_name} đã stream, bỏ qua.')
            return

        # Gửi cảnh báo
        await safe_send_dm(
            member,
            f'⚠️ **Cảnh báo!** Bạn chưa bật stream màn hình trong phòng học.\n'
            f'Bạn sẽ bị kick sau **{WARN_BEFORE_KICK} giây** nếu không bật stream!'
        )
        log.info(f'Đã cảnh báo {member.display_name}, còn {WARN_BEFORE_KICK}s.')

        await asyncio.sleep(WARN_BEFORE_KICK)

        # Kiểm tra lần cuối
        if not (member.voice
                and member.voice.channel
                and member.voice.channel.id in FOCUS_CHANNEL_IDS):
            return

        if not member.voice.self_stream:
            if not bot_can_move(member):
                return
            record_leave(member)
            await member.move_to(None)
            log.info(f'Đã kick {member.display_name} vì không stream.')
            await safe_send_dm(
                member,
                '🚫 Bạn đã bị mời ra khỏi phòng vì **không bật stream màn hình**.\n'
                'Vui lòng bật stream khi vào lại phòng!'
            )
        else:
            log.info(f'{member.display_name} bật stream kịp, không kick.')

    except asyncio.CancelledError:
        log.info(f'Huỷ task kiểm tra cho {member.display_name}.')
    except Exception as e:
        log.error(f'Lỗi check_stream với {member.display_name}: {e}')
    finally:
        pending_checks.pop(member.id, None)

# ============================================================
# 📅 BÁO CÁO HÀNG NGÀY
# ============================================================
async def send_daily_report():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now()
        target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        data = load_data()
        today = datetime.now().strftime('%Y-%m-%d')

        sorted_data = sorted(
            data.items(),
            key=lambda x: x[1]['daily'].get(today, 0),
            reverse=True
        )

        lines = [f'📊 **Báo cáo học tập ngày {today}**\n']
        has_data = False
        for i, (uid, info) in enumerate(sorted_data, 1):
            today_time = info['daily'].get(today, 0)
            total_time = info.get('total', 0)
            if today_time > 0:
                has_data = True
                medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
                lines.append(
                    f'{medal} **{info["name"]}** — '
                    f'Hôm nay: `{format_time(today_time)}` | '
                    f'Tổng: `{format_time(total_time)}`'
                )

        if not has_data:
            lines.append('😴 Hôm nay chưa có ai học!')

        message = '\n'.join(lines)

        for server in SERVERS:
            channel = bot.get_channel(server['report_channel'])
            if channel:
                await channel.send(message)
                log.info(f'Đã gửi báo cáo → {channel.guild.name} #{channel.name}')
            else:
                log.error(f'Không tìm thấy report channel ID {server["report_channel"]}')

# ============================================================
# 📋 LỆNH BOT
# ============================================================
@bot.command(name='stats')
async def stats(ctx, member: discord.Member = None):
    """!stats hoặc !stats @người_dùng — Xem thống kê thời gian học"""
    target = member or ctx.author
    data = load_data()
    uid = str(target.id)

    if uid not in data:
        await ctx.send(f'❌ **{target.display_name}** chưa có dữ liệu học tập!')
        return

    info = data[uid]
    today = datetime.now().strftime('%Y-%m-%d')
    today_time = info['daily'].get(today, 0)
    total_time = info.get('total', 0)
    recent = sorted(info['daily'].items(), reverse=True)[:7]
    recent_lines = '\n'.join([f'  `{d}`: {format_time(s)}' for d, s in recent])

    await ctx.send(
        f'📊 **Thống kê của {target.display_name}**\n'
        f'🕐 Hôm nay: `{format_time(today_time)}`\n'
        f'📚 Tổng cộng: `{format_time(total_time)}`\n'
        f'📅 7 ngày gần nhất:\n{recent_lines}'
    )

@bot.command(name='leaderboard', aliases=['lb', 'top'])
async def leaderboard(ctx):
    """!leaderboard — Xem bảng xếp hạng hôm nay"""
    data = load_data()
    today = datetime.now().strftime('%Y-%m-%d')

    sorted_data = [
        (uid, info) for uid, info in
        sorted(data.items(), key=lambda x: x[1]['daily'].get(today, 0), reverse=True)
        if info['daily'].get(today, 0) > 0
    ][:10]

    lines = ['🏆 **Bảng xếp hạng hôm nay**\n']
    if not sorted_data:
        lines.append('😴 Hôm nay chưa có ai học!')
    else:
        for i, (uid, info) in enumerate(sorted_data, 1):
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
            lines.append(f'{medal} **{info["name"]}** — `{format_time(info["daily"][today])}`')

    await ctx.send('\n'.join(lines))

@bot.command(name='report')
@commands.has_permissions(administrator=True)
async def manual_report(ctx):
    """!report — Gửi báo cáo ngay (chỉ Admin)"""
    data = load_data()
    today = datetime.now().strftime('%Y-%m-%d')

    sorted_data = sorted(
        data.items(),
        key=lambda x: x[1]['daily'].get(today, 0),
        reverse=True
    )

    lines = [f'📊 **Báo cáo học tập ngày {today}** _(thủ công)_\n']
    has_data = False
    for i, (uid, info) in enumerate(sorted_data, 1):
        today_time = info['daily'].get(today, 0)
        if today_time > 0:
            has_data = True
            medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
            lines.append(
                f'{medal} **{info["name"]}** — '
                f'Hôm nay: `{format_time(today_time)}` | '
                f'Tổng: `{format_time(info.get("total", 0))}`'
            )

    if not has_data:
        lines.append('😴 Hôm nay chưa có ai học!')

    await ctx.send('\n'.join(lines))

# ============================================================
# 🎮 EVENTS
# ============================================================
@bot.event
async def on_ready():
    log.info(f'✅ Bot {bot.user.name} đã sẵn sàng!')
    log.info(f'📡 Đang theo dõi {len(FOCUS_CHANNEL_IDS)} phòng voice.')
    bot.loop.create_task(send_daily_report())

    # Kiểm tra user đang trong phòng khi bot restart
    for channel_id in FOCUS_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel:
            for member in channel.members:
                if not member.bot and not member.voice.self_stream:
                    record_join(member)
                    start_check(member, 'đang trong phòng lúc bot khởi động')

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    joined_focus    = after.channel  and after.channel.id  in FOCUS_CHANNEL_IDS
    left_focus      = before.channel and before.channel.id in FOCUS_CHANNEL_IDS
    stayed_in_focus = joined_focus and left_focus
    stream_off      = stayed_in_focus and before.self_stream and not after.self_stream

    if stream_off:
        # Tắt stream trong phòng → đếm ngược lại
        start_check(member, 'tắt stream')

    elif joined_focus and not stayed_in_focus:
        # Vào phòng mới
        record_join(member)
        await safe_send_dm(member, random.choice(MOTIVATIONS))
        if after.self_stream:
            log.info(f'{member.display_name} vào phòng và đã stream sẵn.')
            return
        start_check(member, 'vào phòng')

    elif left_focus and not stayed_in_focus:
        # Rời phòng
        duration = record_leave(member)
        cancel_task(member.id)
        log.info(f'{member.display_name} rời phòng sau {format_time(duration)}.')

bot.run(TOKEN)
