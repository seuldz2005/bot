import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import logging
import os
import json
import random
import anthropic
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string
import threading

# ============================================================
# ⚙️ CẤU HÌNH
# ============================================================
load_dotenv()
TOKEN             = os.getenv('DISCORD_TOKEN')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')   # Thêm vào .env

SERVERS = [
    {
        'voice_channels': [1483271561036435660, 1483301292427186358],
        'report_channel': 1483288436369653861,
    },
    {
        'voice_channels': [1483284081872601098],
        'report_channel': 1483284081872601093,
    },
]

WARN_BEFORE_KICK  = 10      # Cảnh báo trước X giây
WAIT_SECONDS      = 60      # Thời gian chờ trước khi kick
REPORT_HOUR       = 23      # Giờ gửi báo cáo
REPORT_MINUTE     = 0       # Phút gửi báo cáo
DATA_FILE         = 'study_data.json'
DASHBOARD_PORT    = 5000    # Cổng web dashboard
ABSENT_DAYS_WARN  = 2       # Cảnh báo sau X ngày vắng mặt

# 🎮 Hệ thống XP & Level
XP_PER_MINUTE   = 10
LEVEL_THRESHOLDS = [0, 100, 300, 600, 1000, 1500, 2500, 4000, 6000, 9000, 13000]
LEVEL_NAMES      = [
    'Người mới 🌱', 'Học sinh 📖', 'Chăm chỉ ✏️', 'Tập trung 🎯',
    'Xuất sắc ⭐', 'Tinh anh 💎', 'Huyền thoại 🔮', 'Bậc thầy 🧠',
    'Thiên tài 🚀', 'Vô địch 👑', 'Thần học ⚡'
]

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
intents.voice_states   = True
intents.members        = True
intents.message_content = True
bot       = commands.Bot(command_prefix='!', intents=intents)
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

pending_checks: dict[int, asyncio.Task] = {}
join_times: dict[int, datetime]         = {}

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

def _default_user(name: str) -> dict:
    return {
        'name': name, 'daily': {}, 'total': 0,
        'xp': 0, 'level': 0, 'streak': 0,
        'longest_streak': 0, 'last_study_date': '',
        'goal': None, 'goal_seconds': 0,
        'last_absent_warn': ''
    }

def get_level(xp: int) -> int:
    for i in range(len(LEVEL_THRESHOLDS) - 1, -1, -1):
        if xp >= LEVEL_THRESHOLDS[i]:
            return i
    return 0

def xp_to_next_level(xp: int) -> tuple[int, int]:
    level = get_level(xp)
    if level >= len(LEVEL_THRESHOLDS) - 1:
        return level, 0
    return level, LEVEL_THRESHOLDS[level + 1] - xp

def _update_streak(data: dict, uid: str, today: str) -> tuple[int, bool]:
    """Trả về (streak, is_new_day)"""
    yesterday  = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    last_date  = data[uid].get('last_study_date', '')
    streak     = data[uid].get('streak', 0)

    if last_date == today:
        return streak, False        # Đã học hôm nay rồi
    elif last_date == yesterday:
        streak += 1                 # Tiếp tục streak
    else:
        streak = 1                  # Reset streak

    data[uid]['streak']         = streak
    data[uid]['longest_streak'] = max(streak, data[uid].get('longest_streak', 0))
    data[uid]['last_study_date'] = today
    return streak, True

def add_study_time(member_id: int, member_name: str, seconds: int) -> dict:
    """Lưu thời gian học và trả về thông tin phiên học."""
    if seconds <= 0:
        return {}

    data  = load_data()
    today = datetime.now().strftime('%Y-%m-%d')
    uid   = str(member_id)

    if uid not in data:
        data[uid] = _default_user(member_name)
    data[uid]['name'] = member_name

    # Cập nhật thời gian
    data[uid]['daily'][today]  = data[uid]['daily'].get(today, 0) + seconds
    data[uid]['total']         = data[uid].get('total', 0) + seconds

    # XP
    xp_gained = (seconds // 60) * XP_PER_MINUTE
    old_xp    = data[uid].get('xp', 0)
    old_level = get_level(old_xp)

    # Streak bonus
    streak, is_new_day = _update_streak(data, uid, today)
    if is_new_day and streak > 1:
        streak_bonus  = streak * 5
        xp_gained    += streak_bonus
        log.info(f'Streak bonus +{streak_bonus} XP cho {member_name} (streak {streak})')

    data[uid]['xp']    = old_xp + xp_gained
    new_level          = get_level(data[uid]['xp'])
    data[uid]['level'] = new_level

    save_data(data)
    log.info(f'Đã lưu {format_time(seconds)}, +{xp_gained} XP cho {member_name}')

    return {
        'xp_gained':   xp_gained,
        'level_up':    new_level > old_level,
        'new_level':   new_level,
        'streak':      streak,
        'total_xp':    data[uid]['xp'],
        'goal':        data[uid].get('goal'),
        'goal_seconds': data[uid].get('goal_seconds', 0),
        'today_seconds': data[uid]['daily'].get(today, 0),
    }

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

# ============================================================
# 🌐 WEB DASHBOARD
# ============================================================
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📚 Study Bot Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { background:#0f172a; color:#e2e8f0; font-family:'Segoe UI',sans-serif; }
        .card { background:#1e293b; border:1px solid #334155; }
        .xp-bar { background:linear-gradient(90deg,#6366f1,#8b5cf6); }
        .pulse { animation:pulse 2s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
    </style>
</head>
<body class="min-h-screen p-6">
    <div class="max-w-5xl mx-auto">
        <div class="text-center mb-8">
            <h1 class="text-4xl font-bold text-indigo-400">📚 Study Bot Dashboard</h1>
            <p class="text-gray-400 mt-1 text-sm" id="lastUpdate">Đang tải...</p>
        </div>

        <!-- Summary cards -->
        <div id="summaryCards" class="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6"></div>

        <!-- Leaderboard -->
        <div class="card rounded-2xl p-6 mb-6">
            <h2 class="text-xl font-semibold text-indigo-300 mb-4">🏆 Bảng xếp hạng hôm nay</h2>
            <div id="leaderboard"><div class="text-gray-500 pulse">Đang tải...</div></div>
        </div>

        <!-- Chart -->
        <div class="card rounded-2xl p-6">
            <h2 class="text-xl font-semibold text-indigo-300 mb-4">📈 Tổng giờ học 7 ngày qua</h2>
            <canvas id="weekChart" height="100"></canvas>
        </div>
    </div>

<script>
let chartInstance = null;

function fmtTime(s) {
    if (!s || s <= 0) return '0m';
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function getToday() {
    return new Date().toISOString().split('T')[0];
}

function getPastDays(n) {
    return Array.from({length:n}, (_,i) => {
        const d = new Date(); d.setDate(d.getDate() - (n-1-i));
        return d.toISOString().split('T')[0];
    });
}

const LEVEL_NAMES = ['Người mới','Học sinh','Chăm chỉ','Tập trung','Xuất sắc',
                     'Tinh anh','Huyền thoại','Bậc thầy','Thiên tài','Vô địch','Thần học'];
const THRESHOLDS  = [0,100,300,600,1000,1500,2500,4000,6000,9000,13000];

function getLevel(xp) {
    for (let i=THRESHOLDS.length-1;i>=0;i--) if (xp>=THRESHOLDS[i]) return i;
    return 0;
}

function xpPct(xp) {
    const lvl=getLevel(xp);
    if (lvl>=THRESHOLDS.length-1) return 100;
    const cur=THRESHOLDS[lvl], nxt=THRESHOLDS[lvl+1];
    return Math.round(((xp-cur)/(nxt-cur))*100);
}

async function loadData() {
    const res  = await fetch('/api/stats');
    const data = await res.json();
    const today = getToday();
    const users = Object.values(data);

    // Summary cards
    const totalSecs   = users.reduce((s,u)=>s+(u.daily[today]||0),0);
    const activeCount = users.filter(u=>(u.daily[today]||0)>0).length;
    const topStreak   = users.reduce((m,u)=>Math.max(m,u.streak||0),0);
    document.getElementById('summaryCards').innerHTML = `
        <div class="card rounded-2xl p-6 text-center">
            <div class="text-4xl font-bold text-indigo-400">${fmtTime(totalSecs)}</div>
            <div class="text-gray-400 mt-2 text-sm">Tổng giờ học hôm nay</div>
        </div>
        <div class="card rounded-2xl p-6 text-center">
            <div class="text-4xl font-bold text-green-400">${activeCount}</div>
            <div class="text-gray-400 mt-2 text-sm">Người học hôm nay</div>
        </div>
        <div class="card rounded-2xl p-6 text-center">
            <div class="text-4xl font-bold text-orange-400">${topStreak} 🔥</div>
            <div class="text-gray-400 mt-2 text-sm">Streak cao nhất</div>
        </div>
    `;

    // Leaderboard
    const sorted = Object.entries(data)
        .filter(([,u])=>(u.daily[today]||0)>0)
        .sort(([,a],[,b])=>(b.daily[today]||0)-(a.daily[today]||0))
        .slice(0,10);
    const medals = ['🥇','🥈','🥉'];
    document.getElementById('leaderboard').innerHTML = sorted.length===0
        ? '<p class="text-gray-500">😴 Hôm nay chưa có ai học!</p>'
        : sorted.map(([,u],i)=>{
            const lvl=u.level||0, pct=xpPct(u.xp||0);
            const goal=u.goal_seconds>0?Math.min(100,Math.round(((u.daily[today]||0)/u.goal_seconds)*100)):null;
            return `
            <div class="flex items-center gap-4 py-3 px-2 rounded-xl hover:bg-slate-700 transition">
                <div class="text-2xl w-8 text-center">${medals[i]||`${i+1}.`}</div>
                <div class="flex-1 min-w-0">
                    <div class="font-semibold truncate">${u.name}</div>
                    <div class="text-xs text-gray-400">Lv.${lvl} ${LEVEL_NAMES[lvl]} · ${u.xp||0} XP · 🔥${u.streak||0} ngày</div>
                    <div class="w-full bg-slate-700 rounded-full h-1.5 mt-1">
                        <div class="xp-bar h-1.5 rounded-full transition-all" style="width:${pct}%"></div>
                    </div>
                    ${goal!==null?`<div class="text-xs text-yellow-400 mt-0.5">🎯 Mục tiêu: ${goal}%</div>`:''}
                </div>
                <div class="text-indigo-300 font-mono font-bold text-right">${fmtTime(u.daily[today]||0)}</div>
            </div>`;
        }).join('');

    // Chart
    const days   = getPastDays(7);
    const totals = days.map(d=>Math.round(Object.values(data).reduce((s,u)=>s+(u.daily[d]||0),0)/60));
    if (chartInstance) { chartInstance.destroy(); }
    chartInstance = new Chart(document.getElementById('weekChart'), {
        type:'bar',
        data:{
            labels: days.map(d=>d.slice(5)),
            datasets:[{
                label:'Phút học',
                data:totals,
                backgroundColor:'rgba(99,102,241,0.7)',
                borderColor:'#6366f1',
                borderWidth:2,
                borderRadius:8
            }]
        },
        options:{
            responsive:true,
            plugins:{legend:{labels:{color:'#e2e8f0'}}},
            scales:{
                y:{ticks:{color:'#94a3b8'},grid:{color:'#334155'}},
                x:{ticks:{color:'#94a3b8'},grid:{color:'#334155'}}
            }
        }
    });

    document.getElementById('lastUpdate').textContent =
        'Cập nhật lần cuối: ' + new Date().toLocaleTimeString('vi-VN');
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>'''

flask_app = Flask(__name__)

@flask_app.route('/')
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@flask_app.route('/api/stats')
def api_stats():
    return jsonify(load_data())

def run_dashboard():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    flask_app.run(host='0.0.0.0', port=DASHBOARD_PORT, debug=False, use_reloader=False)

# ============================================================
# 🛠️ HÀM TIỆN ÍCH
# ============================================================
async def safe_send_dm(member: discord.Member, message: str):
    try:
        await member.send(message)
    except discord.Forbidden:
        log.warning(f'Không thể gửi DM cho {member.display_name} (chặn DM)')
    except discord.HTTPException as e:
        log.error(f'Lỗi HTTP gửi DM: {e}')

def bot_can_move(member: discord.Member) -> bool:
    if not member.guild.me.guild_permissions.move_members:
        log.error(f'Bot thiếu quyền Move Members!')
        return False
    return True

def record_join(member: discord.Member):
    join_times[member.id] = datetime.now()
    log.info(f'{member.display_name} bắt đầu học lúc {join_times[member.id].strftime("%H:%M:%S")}')

async def record_leave_and_notify(member: discord.Member) -> int:
    """Lưu dữ liệu và gửi DM tóm tắt phiên học."""
    if member.id not in join_times:
        return 0

    duration     = int((datetime.now() - join_times.pop(member.id)).total_seconds())
    session_info = add_study_time(member.id, member.display_name, duration)

    if duration > 30 and session_info:  # Chỉ notify nếu học > 30 giây
        xp          = session_info.get('xp_gained', 0)
        streak      = session_info.get('streak', 0)
        new_level   = session_info.get('new_level', 0)
        goal        = session_info.get('goal')
        goal_secs   = session_info.get('goal_seconds', 0)
        today_secs  = session_info.get('today_seconds', 0)
        level_up    = session_info.get('level_up', False)
        _, xp_need  = xp_to_next_level(session_info.get('total_xp', 0))

        msg = (
            f'✅ **Phiên học kết thúc!**\n'
            f'──────────────────\n'
            f'⏱️ Phiên này: `{format_time(duration)}`\n'
            f'📅 Hôm nay tổng: `{format_time(today_secs)}`\n'
            f'⚡ XP nhận được: `+{xp} XP`\n'
            f'📊 Level: `Lv.{new_level} {LEVEL_NAMES[new_level]}`'
            f' _(còn {xp_need} XP để lên level)_\n'
            f'🔥 Streak: `{streak} ngày liên tiếp`'
        )

        if goal and goal_secs > 0:
            progress = min(100, int((today_secs / goal_secs) * 100))
            bar      = '█' * (progress // 10) + '░' * (10 - progress // 10)
            msg += (
                f'\n──────────────────\n'
                f'🎯 Mục tiêu: **"{goal}"**\n'
                f'`{bar}` {progress}% ({format_time(today_secs)}/{format_time(goal_secs)})'
            )

        if level_up:
            msg += f'\n\n🎉 **LEVEL UP!** Bạn đã lên **Lv.{new_level} {LEVEL_NAMES[new_level]}**! Xuất sắc! 🎊'

        await safe_send_dm(member, msg)

    return duration

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

        if not (member.voice and member.voice.channel
                and member.voice.channel.id in FOCUS_CHANNEL_IDS):
            return
        if member.voice.self_stream:
            log.info(f'{member.display_name} đã stream, bỏ qua.')
            return

        await safe_send_dm(member,
            f'⚠️ **Cảnh báo!** Bạn chưa bật stream màn hình trong phòng học.\n'
            f'Bạn sẽ bị kick sau **{WARN_BEFORE_KICK} giây** nếu không bật stream!'
        )
        await asyncio.sleep(WARN_BEFORE_KICK)

        if not (member.voice and member.voice.channel
                and member.voice.channel.id in FOCUS_CHANNEL_IDS):
            return

        if not member.voice.self_stream:
            if not bot_can_move(member):
                return
            await record_leave_and_notify(member)
            await member.move_to(None)
            log.info(f'Đã kick {member.display_name} vì không stream.')
            await safe_send_dm(member,
                '🚫 Bạn đã bị mời ra khỏi phòng vì **không bật stream màn hình**.\n'
                'Vui lòng bật stream khi vào lại phòng!'
            )
        else:
            log.info(f'{member.display_name} bật stream kịp, không kick.')

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f'Lỗi check_stream với {member.display_name}: {e}')
    finally:
        pending_checks.pop(member.id, None)

# ============================================================
# 📅 SCHEDULED TASKS
# ============================================================
@tasks.loop(minutes=1)
async def scheduled_tasks():
    now = datetime.now()

    # ── Báo cáo cuối ngày ──
    if now.hour == REPORT_HOUR and now.minute == REPORT_MINUTE:
        await _send_report()

    # ── Cảnh báo vắng mặt (chạy lúc 9 giờ sáng) ──
    if now.hour == 9 and now.minute == 0:
        await _check_absences()

async def _send_report():
    data = load_data()
    today = datetime.now().strftime('%Y-%m-%d')
    sorted_data = sorted(data.items(), key=lambda x: x[1]['daily'].get(today, 0), reverse=True)

    lines = [f'📊 **Báo cáo học tập ngày {today}**\n']
    has_data = False
    for i, (uid, info) in enumerate(sorted_data, 1):
        today_time = info['daily'].get(today, 0)
        if today_time > 0:
            has_data = True
            medal  = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
            level  = info.get('level', 0)
            streak = info.get('streak', 0)
            lines.append(
                f'{medal} **{info["name"]}** `Lv.{level}` 🔥{streak} — '
                f'Hôm nay: `{format_time(today_time)}` | '
                f'Tổng: `{format_time(info.get("total", 0))}`'
            )

    if not has_data:
        lines.append('😴 Hôm nay chưa có ai học!')

    message = '\n'.join(lines)
    for server in SERVERS:
        channel = bot.get_channel(server['report_channel'])
        if channel:
            await channel.send(message)
            log.info(f'Đã gửi báo cáo → #{channel.name}')

async def _check_absences():
    data      = load_data()
    today     = datetime.now().strftime('%Y-%m-%d')
    warn_date = (datetime.now() - timedelta(days=ABSENT_DAYS_WARN)).strftime('%Y-%m-%d')

    for uid, info in data.items():
        last_date   = info.get('last_study_date', '')
        last_warned = info.get('last_absent_warn', '')
        if not last_date or last_date >= warn_date:
            continue
        if last_warned == today:        # Đã cảnh báo hôm nay rồi
            continue

        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if not member:
                continue
            days_absent = (datetime.now() - datetime.strptime(last_date, '%Y-%m-%d')).days
            await safe_send_dm(member,
                f'😢 **Ơi {member.display_name}!**\n'
                f'Bạn đã **không học trong {days_absent} ngày** rồi!\n'
                f'🔥 Streak hiện tại: `{info.get("streak", 0)} ngày`\n'
                f'💪 Vào phòng học ngay trước khi streak bị reset nhé!'
            )
            # Ghi lại ngày đã cảnh báo để không spam
            data[uid]['last_absent_warn'] = today
            save_data(data)
            break

# ============================================================
# 📋 SLASH COMMANDS  /command
# ============================================================
@bot.tree.command(name='stats', description='Xem thống kê thời gian học của bạn')
@app_commands.describe(member='Thành viên muốn xem (để trống = bản thân)')
async def slash_stats(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data   = load_data()
    uid    = str(target.id)

    if uid not in data:
        await interaction.response.send_message(
            f'❌ **{target.display_name}** chưa có dữ liệu học tập!', ephemeral=True)
        return

    info       = data[uid]
    today      = datetime.now().strftime('%Y-%m-%d')
    today_time = info['daily'].get(today, 0)
    xp         = info.get('xp', 0)
    level      = info.get('level', 0)
    streak     = info.get('streak', 0)
    longest    = info.get('longest_streak', 0)
    _, xp_need = xp_to_next_level(xp)
    goal       = info.get('goal')
    goal_secs  = info.get('goal_seconds', 0)
    recent     = sorted(info['daily'].items(), reverse=True)[:7]
    recent_str = '\n'.join([f'  `{d}`: {format_time(s)}' for d, s in recent])

    msg = (
        f'📊 **Thống kê của {target.display_name}**\n'
        f'🏅 Level: `Lv.{level} {LEVEL_NAMES[level]}`\n'
        f'⚡ XP: `{xp}` _(còn {xp_need} XP để lên level)_\n'
        f'🔥 Streak: `{streak} ngày` _(kỷ lục: {longest} ngày)_\n'
        f'🕐 Hôm nay: `{format_time(today_time)}`\n'
        f'📚 Tổng cộng: `{format_time(info.get("total", 0))}`\n'
    )
    if goal and goal_secs > 0:
        progress = min(100, int((today_time / goal_secs) * 100))
        msg += f'🎯 Mục tiêu: **"{goal}"** — `{progress}%`\n'
    msg += f'📅 7 ngày gần nhất:\n{recent_str}'

    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name='leaderboard', description='Xem bảng xếp hạng hôm nay')
async def slash_leaderboard(interaction: discord.Interaction):
    data      = load_data()
    today     = datetime.now().strftime('%Y-%m-%d')
    top10     = [
        (uid, info) for uid, info in
        sorted(data.items(), key=lambda x: x[1]['daily'].get(today, 0), reverse=True)
        if info['daily'].get(today, 0) > 0
    ][:10]

    lines = ['🏆 **Bảng xếp hạng hôm nay**\n']
    if not top10:
        lines.append('😴 Hôm nay chưa có ai học!')
    else:
        medals = ['🥇', '🥈', '🥉']
        for i, (uid, info) in enumerate(top10, 1):
            medal  = medals[i - 1] if i <= 3 else f'`{i}.`'
            level  = info.get('level', 0)
            streak = info.get('streak', 0)
            lines.append(
                f'{medal} **{info["name"]}** `Lv.{level}` 🔥{streak} — '
                f'`{format_time(info["daily"][today])}`'
            )
    await interaction.response.send_message('\n'.join(lines))


@bot.tree.command(name='setgoal', description='Đặt mục tiêu học tập hằng ngày')
@app_commands.describe(
    goal='Mô tả mục tiêu (VD: Học Python)',
    hours='Số giờ mục tiêu',
    minutes='Số phút mục tiêu'
)
async def slash_setgoal(
    interaction: discord.Interaction,
    goal: str,
    hours: int = 0,
    minutes: int = 0
):
    total = hours * 3600 + minutes * 60
    if total <= 0:
        await interaction.response.send_message(
            '❌ Vui lòng nhập ít nhất 1 phút!', ephemeral=True)
        return

    data = load_data()
    uid  = str(interaction.user.id)
    if uid not in data:
        data[uid] = _default_user(interaction.user.display_name)
    data[uid]['goal']         = goal
    data[uid]['goal_seconds'] = total
    save_data(data)

    await interaction.response.send_message(
        f'✅ Đã đặt mục tiêu!\n'
        f'🎯 **"{goal}"** — {format_time(total)}/ngày\n'
        f'Cố lên! Bạn có thể làm được! 💪',
        ephemeral=True
    )


@bot.tree.command(name='ask', description='Hỏi AI về bất kỳ điều gì liên quan đến học tập')
@app_commands.describe(question='Câu hỏi của bạn')
async def slash_ask(interaction: discord.Interaction, question: str):
    if not ai_client:
        await interaction.response.send_message(
            '❌ Chức năng AI chưa được cấu hình (thiếu `ANTHROPIC_API_KEY` trong .env).',
            ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        response = ai_client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1000,
            system=(
                'Bạn là trợ lý học tập thông minh trong một Discord server học tập. '
                'Trả lời ngắn gọn, dễ hiểu bằng tiếng Việt. '
                'Dùng emoji phù hợp. Tối đa 400 từ.'
            ),
            messages=[{'role': 'user', 'content': question}]
        )
        answer = response.content[0].text
        msg    = f'🤖 **Câu hỏi:** {question}\n\n📝 **Trả lời:**\n{answer}'
        if len(msg) > 2000:
            msg = msg[:1990] + '...'
        await interaction.followup.send(msg)
    except Exception as e:
        log.error(f'Lỗi AI: {e}')
        await interaction.followup.send('❌ Có lỗi xảy ra khi gọi AI. Thử lại sau nhé!')


@bot.tree.command(name='report', description='Gửi báo cáo ngay (chỉ Admin)')
@app_commands.default_permissions(administrator=True)
async def slash_report(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await _send_report()
    await interaction.followup.send('✅ Đã gửi báo cáo!', ephemeral=True)


# ============================================================
# 📋 PREFIX COMMANDS  !command  (giữ lại để tương thích)
# ============================================================
@bot.command(name='stats')
async def cmd_stats(ctx, member: discord.Member = None):
    """!stats hoặc !stats @người_dùng"""
    target = member or ctx.author
    data   = load_data()
    uid    = str(target.id)

    if uid not in data:
        await ctx.send(f'❌ **{target.display_name}** chưa có dữ liệu!')
        return

    info       = data[uid]
    today      = datetime.now().strftime('%Y-%m-%d')
    level      = info.get('level', 0)
    xp         = info.get('xp', 0)
    streak     = info.get('streak', 0)
    _, xp_need = xp_to_next_level(xp)
    recent     = sorted(info['daily'].items(), reverse=True)[:7]
    recent_str = '\n'.join([f'  `{d}`: {format_time(s)}' for d, s in recent])

    await ctx.send(
        f'📊 **Thống kê của {target.display_name}**\n'
        f'🏅 `Lv.{level} {LEVEL_NAMES[level]}` | ⚡{xp} XP _(còn {xp_need} XP)_\n'
        f'🔥 Streak: `{streak} ngày`\n'
        f'🕐 Hôm nay: `{format_time(info["daily"].get(today, 0))}`\n'
        f'📚 Tổng: `{format_time(info.get("total", 0))}`\n'
        f'📅 7 ngày gần:\n{recent_str}'
    )


@bot.command(name='leaderboard', aliases=['lb', 'top'])
async def cmd_leaderboard(ctx):
    """!leaderboard — Bảng xếp hạng hôm nay"""
    data  = load_data()
    today = datetime.now().strftime('%Y-%m-%d')
    top10 = [
        (uid, info) for uid, info in
        sorted(data.items(), key=lambda x: x[1]['daily'].get(today, 0), reverse=True)
        if info['daily'].get(today, 0) > 0
    ][:10]

    lines = ['🏆 **Bảng xếp hạng hôm nay**\n']
    if not top10:
        lines.append('😴 Hôm nay chưa có ai học!')
    else:
        for i, (uid, info) in enumerate(top10, 1):
            medal  = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'`{i}.`'
            lines.append(
                f'{medal} **{info["name"]}** `Lv.{info.get("level",0)}` '
                f'🔥{info.get("streak",0)} — `{format_time(info["daily"][today])}`'
            )
    await ctx.send('\n'.join(lines))


@bot.command(name='setgoal')
async def cmd_setgoal(ctx, hours: int = 0, minutes: int = 0, *, goal: str = ''):
    """!setgoal <giờ> <phút> <mô tả>  —  VD: !setgoal 2 30 Học Python"""
    if not goal:
        await ctx.send('❌ Dùng: `!setgoal <giờ> <phút> <mô tả>`\nVD: `!setgoal 2 30 Học Python`')
        return
    total = hours * 3600 + minutes * 60
    if total <= 0:
        await ctx.send('❌ Vui lòng nhập ít nhất 1 phút!')
        return
    data = load_data()
    uid  = str(ctx.author.id)
    if uid not in data:
        data[uid] = _default_user(ctx.author.display_name)
    data[uid]['goal']         = goal
    data[uid]['goal_seconds'] = total
    save_data(data)
    await ctx.send(f'✅ Đã đặt mục tiêu **"{goal}"** — {format_time(total)}/ngày! 💪')


@bot.command(name='ask')
async def cmd_ask(ctx, *, question: str = ''):
    """!ask <câu hỏi>  —  Hỏi AI về bất kỳ điều gì"""
    if not ai_client:
        await ctx.send('❌ Chức năng AI chưa được cấu hình (thiếu `ANTHROPIC_API_KEY`).')
        return
    if not question:
        await ctx.send('❌ Dùng: `!ask <câu hỏi của bạn>`')
        return
    async with ctx.typing():
        try:
            response = ai_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=1000,
                system=(
                    'Bạn là trợ lý học tập thông minh. '
                    'Trả lời ngắn gọn, dễ hiểu bằng tiếng Việt. '
                    'Dùng emoji phù hợp. Tối đa 400 từ.'
                ),
                messages=[{'role': 'user', 'content': question}]
            )
            answer = response.content[0].text
            msg    = f'🤖 **Q:** {question}\n\n📝 **A:**\n{answer}'
            if len(msg) > 2000:
                msg = msg[:1990] + '...'
            await ctx.send(msg)
        except Exception as e:
            log.error(f'Lỗi AI: {e}')
            await ctx.send('❌ Có lỗi xảy ra. Thử lại sau!')


@bot.command(name='report')
@commands.has_permissions(administrator=True)
async def cmd_report(ctx):
    """!report — Gửi báo cáo ngay (Admin only)"""
    await _send_report()
    await ctx.send('✅ Đã gửi báo cáo!')


# ============================================================
# 🎮 EVENTS
# ============================================================
@bot.event
async def on_ready():
    log.info(f'✅ Bot {bot.user.name} đã sẵn sàng!')
    log.info(f'📡 Đang theo dõi {len(FOCUS_CHANNEL_IDS)} phòng voice.')

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        log.info(f'✅ Đã sync {len(synced)} slash commands')
    except Exception as e:
        log.error(f'Lỗi sync slash commands: {e}')

    # Start scheduled tasks
    if not scheduled_tasks.is_running():
        scheduled_tasks.start()

    # Start web dashboard in background thread
    threading.Thread(target=run_dashboard, daemon=True).start()
    log.info(f'🌐 Dashboard chạy tại http://localhost:{DASHBOARD_PORT}')

    # Kiểm tra user đang trong phòng khi bot restart
    for channel_id in FOCUS_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel:
            for member in channel.members:
                if not member.bot and not member.voice.self_stream:
                    record_join(member)
                    start_check(member, 'đang trong phòng lúc bot khởi động')


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):
    if member.bot:
        return

    joined_focus    = after.channel  and after.channel.id  in FOCUS_CHANNEL_IDS
    left_focus      = before.channel and before.channel.id in FOCUS_CHANNEL_IDS
    stayed_in_focus = joined_focus and left_focus
    stream_off      = stayed_in_focus and before.self_stream and not after.self_stream

    if stream_off:
        start_check(member, 'tắt stream')

    elif joined_focus and not stayed_in_focus:
        record_join(member)
        await safe_send_dm(member, random.choice(MOTIVATIONS))
        if after.self_stream:
            log.info(f'{member.display_name} vào phòng và đã stream sẵn.')
            return
        start_check(member, 'vào phòng')

    elif left_focus and not stayed_in_focus:
        duration = await record_leave_and_notify(member)
        cancel_task(member.id)
        log.info(f'{member.display_name} rời phòng sau {format_time(duration)}.')


bot.run(TOKEN)