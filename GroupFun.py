# encoding:utf-8
import plugins
import sqlite3
from datetime import datetime, timedelta
import os
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
from common.log import logger
from plugins import *
from collections import defaultdict

@plugins.register(
    name="GroupFun",
    desire_priority=89,
    hidden=True,
    desc="群聊娱乐中心(梗王/水王/夜猫子/早起鸟)",
    version="3.3",
    author="yishuizhe",
)

class GroupFun(Plugin):
    def __init__(self):
        super().__init__()
        self.curdir = os.path.dirname(__file__)
        # 确保目录存在
        os.makedirs(self.curdir, exist_ok=True)
        self.db_path = os.path.join(self.curdir, "fun_center.db")
        
        # 成就配置
        self.ACHIEVEMENTS = {
            "meme_lord": {"name": "🤪梗王", "condition": 10, "desc": "原创梗被引用10次以上"},
            "water_king": {"name": "🏆水王", "condition": 50, "desc": "单日发言超过50条"},
            "night_owl": {"name": "🌙夜猫子", "condition": 3, "desc": "凌晨0-5点发言3次"},
            "early_bird": {"name": "🐦早起鸟", "condition": 3, "desc": "早上6-8点发言3次"}
        }
        
        try:
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()
            self.max_record_days = self.config.get("max_record_days", 30)  # 默认保留30天
            self.init_database()
            logger.info("[GroupFun] inited")
            
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            self.handlers[Event.ON_RECEIVE_MESSAGE] = self.on_receive_message
        except Exception as e:
            logger.error(f"[GroupFun] 初始化异常：{e}", exc_info=True)
            raise RuntimeError("[GroupFun] init failed, ignore") from e

    def init_database(self):
        """初始化数据库"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                cursor = conn.cursor()
                
                # 聊天记录表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS chat_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        user_nickname TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        content TEXT,
                        create_time TEXT NOT NULL,
                        hour_group INTEGER
                    )''')
                
                # 梗词典表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS meme_dict (
                        group_id TEXT NOT NULL,
                        meme_text TEXT NOT NULL,
                        creator TEXT NOT NULL,
                        creator_id TEXT NOT NULL,
                        usage_count INTEGER DEFAULT 1,
                        create_time TEXT NOT NULL,
                        PRIMARY KEY (group_id, meme_text)
                    )''')
                
                # 用户梗数统计表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS user_meme_stats (
                        user_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        meme_count INTEGER DEFAULT 0,
                        PRIMARY KEY (user_id, group_id)
                    )''')
                
                # 成就表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS user_achievements (
                        user_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        achievement_id TEXT NOT NULL,
                        unlock_time TEXT NOT NULL,
                        PRIMARY KEY (user_id, group_id, achievement_id)
                    )''')
                
                # 时段统计表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS hour_stats (
                        user_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        hour_group INTEGER NOT NULL,
                        count INTEGER DEFAULT 0,
                        date TEXT NOT NULL,
                        PRIMARY KEY (user_id, group_id, hour_group, date)
                    )''')
                
                # 自动清理旧数据
                cutoff = (datetime.now() - timedelta(days=self.max_record_days)).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute("DELETE FROM chat_records WHERE create_time < ?", (cutoff,))
                logger.info(f"已清理 {self.max_record_days} 天前的聊天记录")
                
                conn.commit()
                logger.info("数据库表创建完成")
        except sqlite3.Error as e:
            logger.critical(f"数据库初始化失败: {str(e)}")
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            raise RuntimeError("数据库初始化失败，请检查日志")

    def on_handle_context(self, e_context: EventContext):
        """处理命令"""
        if e_context["context"].type != ContextType.TEXT:
            return
            
        msg: ChatMessage = e_context["context"]["msg"]
        content = e_context["context"].content.strip().lower()
        
        if not e_context["context"]["isgroup"]:
            return
            
        reply = Reply()
        reply.type = ReplyType.TEXT
        
        try:
            # 1. 水王排行功能
            if content.startswith("今日水王"):
                reply.content = self.get_water_king(msg.other_user_id)
            elif content.startswith("本周水王"):
                reply.content = self.get_water_king(msg.other_user_id, "week")
            elif content.startswith("本月水王"):
                reply.content = self.get_water_king(msg.other_user_id, "month")
            
            # 2. 梗百科功能
            elif content.startswith("梗百科") or content.startswith("梗排行榜"):
                reply.content = self.get_meme_rank(msg.other_user_id)
            
            # 3. 成就系统
            elif content.startswith("我的成就"):
                reply.content = self.get_user_achievements(msg.other_user_id, msg.actual_user_id)
            else:
                return
                
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
        except Exception as e:
            logger.error(f"[GroupFun]处理命令异常：{e}")
            reply.content = "功能暂时不可用"
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def on_receive_message(self, e_context: EventContext):
        """处理所有消息，用于数据收集"""
        if e_context["context"].type != ContextType.TEXT:
            return
            
        msg: ChatMessage = e_context["context"]["msg"]
        
        if not msg.is_group:
            return
            
        try:
            # 保存聊天记录
            self.save_message(msg)
            
            # 检测梗（短消息或重复消息）
            if self.is_potential_meme(msg.content):
                self.check_meme_creation(msg)
            
            # 检查水王成就
            self.check_water_king(msg)
            
            # 检查时段成就
            hour = datetime.now().hour
            self.check_time_achievements(msg, hour)
            self.update_hour_stats(msg, hour)
        except Exception as e:
            logger.error(f"[GroupFun]处理消息异常：{e}")

    def is_potential_meme(self, content):
        """判断是否是潜在的梗消息"""
        # 排除包含特定关键词的消息
        exclude_keywords = ["梗", "水王", "成就"]
        if any(keyword in content for keyword in exclude_keywords):
            return False
        
        # 短消息(小于50字符)或包含表情符号
        return len(content) <= 50 or any(c in content for c in ["🤪", "😂", "🐶", "🐱"])

    def save_message(self, msg):
        """安全保存消息"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO chat_records 
                    (group_id, user_nickname, user_id, content, create_time, hour_group)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    msg.other_user_id,
                    msg.actual_user_nickname,
                    msg.actual_user_id,
                    msg.content,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    datetime.now().hour
                ))
                conn.commit()
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                logger.warning("检测到旧版数据库，尝试重建...")
                os.remove(self.db_path)
                self.init_database()
                self.save_message(msg)
            else:
                raise

    def check_meme_creation(self, msg):
        """三人成梗检测"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 1. 检查是否已有3人使用
                cursor.execute('''
                    SELECT COUNT(DISTINCT user_id) 
                    FROM chat_records 
                    WHERE group_id = ? AND content = ?
                ''', (msg.other_user_id, msg.content))
                user_count = cursor.fetchone()[0]
                
                if user_count >= 3:
                    # 2. 获取原创者
                    cursor.execute('''
                        SELECT user_nickname, user_id 
                        FROM chat_records 
                        WHERE group_id = ? AND content = ?
                        ORDER BY create_time ASC LIMIT 1
                    ''', (msg.other_user_id, msg.content))
                    creator, creator_id = cursor.fetchone()
                    
                    # 3. 更新梗词典
                    cursor.execute('''
                        INSERT OR IGNORE INTO meme_dict 
                        (group_id, meme_text, creator, creator_id, create_time)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(group_id, meme_text) 
                        DO UPDATE SET usage_count = usage_count + 1
                    ''', (
                        msg.other_user_id,
                        msg.content,
                        creator,
                        creator_id,
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    ))
                    
                    # 4. 更新用户梗数
                    cursor.execute('''
                        INSERT OR IGNORE INTO user_meme_stats
                        (user_id, group_id, meme_count)
                        VALUES (?, ?, 0)
                    ''', (creator_id, msg.other_user_id))
                    
                    cursor.execute('''
                        UPDATE user_meme_stats 
                        SET meme_count = meme_count + 1 
                        WHERE user_id = ? AND group_id = ?
                    ''', (creator_id, msg.other_user_id))
                    
                    # 5. 检查梗王成就
                    cursor.execute('''
                        SELECT meme_count FROM user_meme_stats
                        WHERE user_id = ? AND group_id = ?
                    ''', (creator_id, msg.other_user_id))
                    
                    if cursor.fetchone()[0] >= 10:
                        self.grant_achievement(creator_id, msg.other_user_id, "meme_lord")
                        logger.info(f"[成就] {creator} 成为梗王！")
                    
                    conn.commit()
                    logger.info(f"[新梗] {creator} 的「{msg.content}」被{user_count}人使用")
        except Exception as e:
            logger.error(f"[梗检测异常] {e}", exc_info=True)

    def check_water_king(self, msg):
        """水王检测"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                today = datetime.now().strftime('%Y-%m-%d')
                
                cursor.execute('''
                    SELECT COUNT(*) FROM chat_records 
                    WHERE group_id = ? AND user_id = ? AND date(create_time) = ?
                ''', (msg.other_user_id, msg.actual_user_id, today))
                
                if cursor.fetchone()[0] >= 50:
                    self.grant_achievement(msg.actual_user_id, msg.other_user_id, "water_king")
        except Exception as e:
            logger.error(f"[水王检测异常] {e}")

    def check_time_achievements(self, msg, hour):
        """时段成就检测"""
        try:
            achievement_id = None
            hour_range = None
            
            if 0 <= hour < 5:  # 夜猫子
                achievement_id = "night_owl"
                hour_range = (0, 5)
            elif 6 <= hour < 8:  # 早起鸟
                achievement_id = "early_bird"
                hour_range = (6, 8)
            else:
                return
                
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                today = datetime.now().strftime('%Y-%m-%d')
                
                cursor.execute('''
                    SELECT SUM(count) FROM hour_stats 
                    WHERE user_id = ? AND group_id = ? 
                      AND hour_group BETWEEN ? AND ?
                      AND date = ?
                ''', (
                    msg.actual_user_id,
                    msg.other_user_id,
                    hour_range[0],
                    hour_range[1]-1,
                    today
                ))
                
                count = cursor.fetchone()[0] or 0
                if count >= 3:
                    self.grant_achievement(msg.actual_user_id, msg.other_user_id, achievement_id)
        except Exception as e:
            logger.error(f"[时段成就异常] {e}")

    def grant_achievement(self, user_id, group_id, achievement_id):
        """授予成就"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT OR IGNORE INTO user_achievements
                    (user_id, group_id, achievement_id, unlock_time)
                    VALUES (?, ?, ?, ?)
                ''', (
                    user_id,
                    group_id,
                    achievement_id,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"[成就授予失败] {e}")

    def get_water_king(self, group_id, period="day"):
        """获取水王排行榜"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                if period == "day":
                    date_filter = "date(create_time) = date('now')"
                    title = "今日水王🏆"
                elif period == "week":
                    date_filter = "date(create_time) >= date('now', 'weekday 0', '-7 days')"
                    title = "本周水王🏆"
                elif period == "month":
                    date_filter = "strftime('%Y-%m', create_time) = strftime('%Y-%m', 'now')"
                    title = "本月水王🏆"
                else:
                    return "无效的时间范围"
                
                cursor.execute(f'''
                    SELECT user_nickname, COUNT(*) as count 
                    FROM chat_records 
                    WHERE group_id = ? AND {date_filter}
                    GROUP BY user_id 
                    ORDER BY count DESC 
                    LIMIT 3
                ''', (group_id,))
                
                results = cursor.fetchall()
                if not results:
                    return f"{title.replace('🏆', '')}还没有水王哦~"
                
                rank = [f"【{title}】"]
                medals = ["🥇", "🥈", "🥉"]
                for i, (user, count) in enumerate(results):
                    rank.append(f"{medals[i]} {user}: {count}条")
                
                return "\n".join(rank)
        except Exception as e:
            logger.error(f"[水王榜异常] {e}")
            return "数据获取失败"

    def get_meme_rank(self, group_id):
        """梗排行榜"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                cursor.execute('''
                    SELECT meme_text, creator, usage_count 
                    FROM meme_dict 
                    WHERE group_id = ? 
                    ORDER BY usage_count DESC 
                    LIMIT 10
                ''', (group_id,))
                
                results = cursor.fetchall()
                if not results:
                    return "本群还没有流行梗哦~"
                
                rank = ["【梗王排行榜🤪】"]
                for i, row in enumerate(results, 1):
                    rank.append(f"{i}. {row['meme_text']} (by {row['creator']}, 被引{row['usage_count']}次)")
                
                return "\n".join(rank)
        except Exception as e:
            logger.error(f"[梗榜异常] {e}")
            return "数据获取失败"
    def get_user_achievements(self, group_id, user_id):
        """用户成就查询"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
            
                # 已解锁成就
                unlocked = cursor.execute('''
                    SELECT achievement_id FROM user_achievements
                    WHERE user_id = ? AND group_id = ?
                ''', (user_id, group_id)).fetchall()
                unlocked_ids = {row[0] for row in unlocked}
            
                # 获取进度数据
                today = datetime.now().strftime('%Y-%m-%d')
                progress = []
            
                # 水王进度
                if "water_king" in unlocked_ids:
                    progress.append("🏆水王: 已完成")
                else:
                    cursor.execute('''
                        SELECT COUNT(*) FROM chat_records 
                        WHERE group_id = ? AND user_id = ? AND date(create_time) = ?
                    ''', (group_id, user_id, today))
                    water_count = cursor.fetchone()[0]
                    progress.append(f"🏆水王: {water_count}/50条")
            
                # 夜猫子进度
                if "night_owl" in unlocked_ids:
                    progress.append("🌙夜猫子: 已完成")
                else:
                    cursor.execute('''
                        SELECT SUM(count) FROM hour_stats 
                        WHERE user_id = ? AND group_id = ? 
                        AND hour_group BETWEEN 0 AND 4 
                        AND date = ?
                    ''', (user_id, group_id, today))
                    night_count = cursor.fetchone()[0] or 0
                    progress.append(f"🌙夜猫子: {night_count}/3次")
            
                # 早起鸟进度
                if "early_bird" in unlocked_ids:
                    progress.append("🐦早起鸟: 已完成")
                else:
                    cursor.execute('''
                        SELECT SUM(count) FROM hour_stats 
                        WHERE user_id = ? AND group_id = ? 
                        AND hour_group BETWEEN 6 AND 7 
                        AND date = ?
                    ''', (user_id, group_id, today))
                    morning_count = cursor.fetchone()[0] or 0
                    progress.append(f"🐦早起鸟: {morning_count}/3次")
            
                # 梗王进度
                cursor.execute('''
                    SELECT meme_count FROM user_meme_stats
                    WHERE user_id = ? AND group_id = ?
                ''', (user_id, group_id))
                meme_result = cursor.fetchone()
                meme_count = meme_result[0] if meme_result else 0
            
                if "meme_lord" in unlocked_ids:
                    progress.append("🤪梗王: 已完成")
                else:
                    progress.append(f"🤪梗王: {meme_count}/10个")
                    # 自动检查并授予梗王成就
                    if meme_count >= 10:
                        self.grant_achievement(user_id, group_id, "meme_lord")
                        unlocked_ids.add("meme_lord")
            
                # 构建回复
                lines = ["【我的成就🏅】"]
            
                if unlocked_ids:
                    lines.append("=== 已解锁 ===")
                    for ach_id, ach in self.ACHIEVEMENTS.items():
                        if ach_id in unlocked_ids:
                            lines.append(f"{ach['name']}: {ach['desc']}")
            
                lines.append("\n=== 当前进度 ===")
                lines.extend(progress)
            
                return "\n".join(lines)
        except Exception as e:
            logger.error(f"[成就查询异常] {e}")
            return "成就数据获取失败"

    def update_hour_stats(self, msg, hour):
        """更新时段统计数据"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                today = datetime.now().strftime('%Y-%m-%d')
                conn.execute('''
                    INSERT INTO hour_stats 
                    (user_id, group_id, hour_group, count, date)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(user_id, group_id, hour_group, date) 
                    DO UPDATE SET count = count + 1
                ''', (
                    msg.actual_user_id,
                    msg.other_user_id,
                    hour,
                    today
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"[时段统计更新失败] {e}")

    def get_help_text(self, **kwargs):
        return """
【群聊娱乐中心使用说明】
1. 今日水王 - 查看今日发言排行榜
2. 本周水王 - 查看本周发言排行榜
3. 本月水王 - 查看本月发言排行榜
4. 梗排行榜 - 查看本群流行梗
5. 我的成就 - 查看已获得成就和进度

成就系统：
- 🤪 梗王：原创梗被10人引用
- 🏆 水王：单日发言50条
- 🌙 夜猫子：凌晨0-5点发言3次
- 🐦 早起鸟：早上6-8点发言3次
"""