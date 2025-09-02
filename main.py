import os
import asyncio
from hypercorn.config import Config
from hypercorn.asyncio import serve

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.message.components import At
from astrbot.core.star.filter.permission import PermissionType

# ==========================================================
# 导入重构后的所有模块
# ==========================================================
# 仓储实现
from .core.repositories.sqlite_user_repo import SqliteUserRepository
from .core.repositories.sqlite_item_template_repo import SqliteItemTemplateRepository
from .core.repositories.sqlite_inventory_repo import SqliteInventoryRepository
from .core.repositories.sqlite_gacha_repo import SqliteGachaRepository
from .core.repositories.sqlite_market_repo import SqliteMarketRepository
from .core.repositories.sqlite_log_repo import SqliteLogRepository
from .core.repositories.sqlite_achievement_repo import SqliteAchievementRepository
from .core.services.data_setup_service import DataSetupService
from .core.services.item_template_service import ItemTemplateService
# 服务
from .core.services.user_service import UserService
from .core.services.fishing_service import FishingService
from .core.services.inventory_service import InventoryService
from .core.services.shop_service import ShopService
from .core.services.market_service import MarketService
from .core.services.gacha_service import GachaService
from .core.services.achievement_service import AchievementService
from .core.services.game_mechanics_service import GameMechanicsService
# 其他

from .core.database.migration import run_migrations
from .core.utils import get_now
from .draw.rank import draw_fishing_ranking
from .draw.help import draw_help_image
from .manager.server import create_app
from .utils import get_public_ip, to_percentage, format_accessory_or_rod, safe_datetime_handler, _is_port_available


class FishingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # --- 1. 加载配置 ---
        self.is_tax = config.get("is_tax", True)  # 是否开启税收
        self.threshold = config.get("threshold", 100000)  # 起征点
        self.step_coins = config.get("step_coins", 100000)
        self.step_rate = config.get("step_rate", 0.01)
        self.max_rate = config.get("max_rate", 0.2)  # 最大税率
        self.min_rate = config.get("min_rate", 0.05)  # 最小税率
        self.area2num = config.get("area2num", 2000)
        self.area3num = config.get("area3num", 500)
        self.ten_thousand_gacha_lock = False
        self.ten_thousand_gacha_user = None  # 记录当前进行万连的用户
        self.game_config = {
            "fishing": {"cost": config.get("fish_cost", 10), "cooldown_seconds": config.get("fish_cooldown_seconds", 180)},
            "steal": {"cooldown_seconds": config.get("steal_cooldown_seconds", 14400)},
            "user": {"initial_coins": config.get("user_initial_coins", 200)},
            "market": {"listing_tax_rate": config.get("market_listing_tax_rate", 0.05)},
            "consecutive_bonuses": {
                "7": 1000,  # 连续签到7天奖励1000金币
                "14": 50000,  # 连续签到14天奖励5000金币
                "30": 2000000,  # 连续签到30天奖励2000000金币
                "45": 5000000,  # 连续签到45天奖励5000000金币
                "60": 10000000,  # 连续签到60天奖励10000000金币
                "90": 50000000,  # 连续签到90天奖励50000000金币
                "120": 100000000,  # 连续签到120天奖励100000000金币
            },
            "tax_config":{
                "is_tax": self.is_tax,
                "threshold": self.threshold,  # 起征点
                "step_coins": self.step_coins,  # 每次增加的金币数
                "step_rate": self.step_rate,  # 每次增加的税率
                "max_rate": self.max_rate,  # 最大税率
                "min_rate": self.min_rate,  # 最小税率
            },
            "sell_prices": {
              "by_rarity": {
                  "1": config.get("sell_prices", {"by_rarity_1":100}).get("by_rarity_1", 100),
                  "2": config.get("sell_prices", {"by_rarity_2": 500}).get("by_rarity_2", 500),
                  "3": config.get("sell_prices", {"by_rarity_3": 1000}).get("by_rarity_3", 1000),
                  "4": config.get("sell_prices", {"by_rarity_4": 5000}).get("by_rarity_4", 5000),
                  "5": config.get("sell_prices", {"by_rarity_5": 10000}).get("by_rarity_5", 10000),
              }
            },
            "wipe_bomb": {
                "max_attempts_per_day": 3,
                "reward_ranges": [
                    (0.0, 0.5, 35),  # 0.0-0.5倍，权重35
                    (0.5, 1.0, 25),  # 0.5-1.0倍，权重25
                    (1.0, 2.0, 20),  # 1.0-2.0倍，权重20
                    (2.0, 3.0, 10),  # 2.0-3.0倍，权重10
                    (3.0, 5.0, 7),  # 3.0-5.0倍，权重7
                    (5.0, 8.0, 2),  # 5.0-8.0倍，权重2
                    (8.0, 10.0, 1),  # 8.0-10.0倍，权重1
                ]
            },
            "pond_upgrades": [
                { "from": 480, "to": 999, "cost": 50000 },
                { "from": 999, "to": 9999, "cost": 500000 },
                { "from": 9999, "to": 99999, "cost": 50000000 },
                { "from": 99999, "to": 999999, "cost": 5000000000 },
            ]
        }
        db_path = "data/fish.db"
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # 初始化数据库模式
        plugin_root_dir = os.path.dirname(__file__)
        migrations_path = os.path.join(plugin_root_dir, "core", "database", "migrations")
        run_migrations(db_path, migrations_path)

        # --- 2. 组合根：实例化所有仓储层 ---
        self.user_repo = SqliteUserRepository(db_path)
        self.item_template_repo = SqliteItemTemplateRepository(db_path)
        self.inventory_repo = SqliteInventoryRepository(db_path)
        self.gacha_repo = SqliteGachaRepository(db_path)
        self.market_repo = SqliteMarketRepository(db_path)
        self.log_repo = SqliteLogRepository(db_path)
        self.achievement_repo = SqliteAchievementRepository(db_path)

        # --- 3. 组合根：实例化所有服务层，并注入依赖 ---
        self.user_service = UserService(self.user_repo, self.log_repo, self.inventory_repo, self.item_template_repo, self.game_config)
        self.inventory_service = InventoryService(self.inventory_repo, self.user_repo, self.item_template_repo,
                                                  self.game_config)
        self.shop_service = ShopService(self.item_template_repo, self.inventory_repo, self.user_repo)
        self.market_service = MarketService(self.market_repo, self.inventory_repo, self.user_repo, self.log_repo,
                                            self.item_template_repo, self.game_config)
        self.gacha_service = GachaService(
    self.gacha_repo, 
    self.user_repo, 
    self.inventory_repo, 
    self.item_template_repo,
    self.log_repo, 
    self.achievement_repo,
    self.game_config  # 传入配置
)
        self.game_mechanics_service = GameMechanicsService(self.user_repo, self.log_repo, self.inventory_repo,
                                                           self.item_template_repo, self.game_config)
        self.achievement_service = AchievementService(self.achievement_repo, self.user_repo, self.inventory_repo,
                                                      self.item_template_repo, self.log_repo)
        self.fishing_service = FishingService(self.user_repo, self.inventory_repo, self.item_template_repo,
                                              self.log_repo, self.game_config)

        self.item_template_service = ItemTemplateService(self.item_template_repo, self.gacha_repo)

        # --- 4. 启动后台任务 ---
        self.fishing_service.start_auto_fishing_task()
        self.achievement_service.start_achievement_check_task()

        # --- 5. 初始化核心游戏数据 ---
        data_setup_service = DataSetupService(self.item_template_repo, self.gacha_repo)
        data_setup_service.setup_initial_data()
        self.fishing_service.on_load(area2num=self.area2num, area3num=self.area3num)

        # --- Web后台配置 ---
        self.web_admin_task = None
        self.secret_key = config.get("secret_key", "default_secret_key")
        self.port = config.get("port", 7777)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        logger.info("""
    _____ _     _     _
    |  ___(_)___| |__ (_)_ __   __ _
    | |_  | / __| '_ \\| | '_ \\ / _` |
    |  _| | \\__ \\ | | | | | | | (_| |
    |_|   |_|___/_| |_|_|_| |_|\\__, |
                               |___/
                               """)

    # ===========基础与核心玩法==========
    async def _send_long_message(self, event, message: str, title: str):
        """处理长消息的通用方法，复用鱼类图鉴的逻辑"""
        if len(message) <= 500:
            yield event.plain_result(message)
            return

        text_chunk_size = 1000  # 每个Plain文本块的最大字数
        node_chunk_size = 4  # 每个Node中最多包含的Plain文本块数量
        text_chunks = [message[i:i + text_chunk_size] for i in
                    range(0, len(message), text_chunk_size)]

        if not text_chunks:
            yield event.plain_result("❌ 内容为空，无法发送。")
            return

        grouped_chunks = [text_chunks[i:i + node_chunk_size] for i in
                        range(0, len(text_chunks), node_chunk_size)]

        from astrbot.api.message_components import Node, Plain
        nodes_to_send = []
        for i, group in enumerate(grouped_chunks):
            plain_components = [Plain(text=chunk) for chunk in group]

            node = Node(
                uin=event.get_self_id(),
                name=f"{title} - 第 {i + 1} 页",
                content=plain_components
            )
            nodes_to_send.append(node)

        try:
            yield event.chain_result(nodes_to_send)
        except Exception as e:
            yield event.plain_result(f"❌ 发送转发消息失败：{e}")
        
    @filter.command("注册")
    async def register_user(self, event: AstrMessageEvent):
        """注册用户命令"""
        user_id = event.get_sender_id()
        nickname = event.get_sender_name() if event.get_sender_name() is not None else event.get_sender_id()
        result = self.user_service.register(user_id, nickname)
        if result:
            yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("钓鱼")
    async def fish(self, event: AstrMessageEvent):
        """钓鱼"""
        user_id = event.get_sender_id()
        user = self.user_repo.get_by_id(user_id)
        if not user:
            yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")
            return
        # 检查用户钓鱼CD
        lst_time = user.last_fishing_time
        # 检查是否装备了海洋之心饰品
        info = self.user_service.get_user_current_accessory(user_id)
        if info["success"] is False:
            yield event.plain_result(f"❌ 获取用户饰品信息失败：{info['message']}")
            return
        equipped_accessory = info.get("accessory")
        cooldown_seconds = self.game_config["fishing"]["cooldown_seconds"]
        if equipped_accessory and equipped_accessory.get("name") == "海洋之心":
            # 如果装备了海洋之心，CD时间减半
            cooldown_seconds = self.game_config["fishing"]["cooldown_seconds"] / 2
            # logger.info(f"用户 {user_id} 装备了海洋之心，钓鱼CD时间减半。")
        # 修复时区问题
        now = get_now()
        if lst_time and lst_time.tzinfo is None and now.tzinfo is not None:
            # 如果 lst_time 没有时区而 now 有时区，移除 now 的时区信息
            now = now.replace(tzinfo=None)
        elif lst_time and lst_time.tzinfo is not None and now.tzinfo is None:
            # 如果 lst_time 有时区而 now 没有时区，将 now 转换为有时区
            now = now.replace(tzinfo=lst_time.tzinfo)
        if lst_time and (now - lst_time).total_seconds() < cooldown_seconds:
            wait_time = cooldown_seconds - (now - lst_time).total_seconds()
            yield event.plain_result(f"⏳ 您还需要等待 {int(wait_time)} 秒才能再次钓鱼。")
            return
        result = self.fishing_service.go_fish(user_id)
        if result:
            if result["success"]:
                yield event.plain_result(
                    f"🎣 恭喜你钓到了：{result['fish']['name']}\n✨品质：{'★' * result['fish']['rarity']} \n⚖️重量：{result['fish']['weight']} 克\n💰价值：{result['fish']['value']} 金币")
            else:
                yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("签到")
    async def sign_in(self, event: AstrMessageEvent):
        """签到"""
        user_id = event.get_sender_id()
        result = self.user_service.daily_sign_in(user_id)
        if result["success"]:
            message = f"✅ 签到成功！获得 {result['coins_reward']} 金币。"
            if result["bonus_coins"] > 0:
                message += f"\n🎉 连续签到 {result['consecutive_days']} 天，额外奖励 {result['bonus_coins']} 金币！"
            yield event.plain_result(message)
        else:
            yield event.plain_result(f"❌ 签到失败：{result['message']}")

    @filter.command("自动钓鱼")
    async def auto_fish(self, event: AstrMessageEvent):
        """自动钓鱼"""
        user_id = event.get_sender_id()
        result = self.fishing_service.toggle_auto_fishing(user_id)
        yield event.plain_result(result["message"])

    @filter.command("钓鱼记录", alias={"钓鱼日志", "钓鱼历史"})
    async def fishing_log(self, event: AstrMessageEvent):
        """查看钓鱼记录"""
        user_id = event.get_sender_id()
        result = self.fishing_service.get_user_fish_log(user_id)
        if result:
            if result["success"]:
                records = result["records"]
                if not records:
                    yield event.plain_result("❌ 您还没有钓鱼记录。")
                    return
                message = "【📜 钓鱼记录】：\n"
                for record in records:
                    message += (f" - {record['fish_name']} ({'★' * record['fish_rarity']})\n"
                                f" - ⚖️重量: {record['fish_weight']} 克 - 💰价值: {record['fish_value']} 金币\n"
                                f" - 🔧装备： {record['accessory']} & {record['rod']} | 🎣鱼饵: {record['bait']}\n"
                                f" - 钓鱼时间: {safe_datetime_handler(record['timestamp'])}\n")
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 获取钓鱼记录失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    # ===========背包与资产管理==========

    @filter.command("状态", alias={"用户状态", "查看状态"})
    async def user_status(self, event: AstrMessageEvent):
        """查看用户状态"""
        user_id = event.get_sender_id()
        user = self.user_repo.get_by_id(user_id)
        if user:
            # 导入绘制函数
            from .draw.state import draw_state_image, get_user_state_data
            
            # 获取用户状态数据
            user_data = get_user_state_data(
                self.user_repo,
                self.inventory_repo,
                self.item_template_repo,
                self.log_repo,
                self.game_config,
                user_id
            )
            
            if user_data:
                # 生成状态图像
                image = draw_state_image(user_data)
                # 保存图像到临时文件
                image_path = "user_status.png"
                image.save(image_path)
                yield event.image_result(image_path)
            else:
                yield event.plain_result("❌ 获取用户状态数据失败。")
        else:
            yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")
            
    @filter.command("鱼塘")
    async def pond(self, event: AstrMessageEvent):
        """查看用户鱼塘内的鱼"""
        user_id = event.get_sender_id()
        pond_fish = self.inventory_service.get_user_fish_pond(user_id)
        if pond_fish:
            fishes = pond_fish["fishes"]
            # 把fishes按稀有度分组
            fished_by_rarity = {}
            for fish in fishes:
                rarity = fish.get("rarity", "未知")
                if rarity not in fished_by_rarity:
                    fished_by_rarity[rarity] = []
                fished_by_rarity[rarity].append(fish)
            # 构造输出信息
            message = "【🐠 鱼塘】：\n"
            for rarity in sorted(fished_by_rarity.keys(), reverse=True):
                fish_list = fished_by_rarity[rarity]
                if fish_list:
                    message += f"\n {'⭐' * rarity } 稀有度 {rarity}：\n"
                    for fish in fish_list:
                        message += f"  - {fish['name']} x  {fish['quantity']} （{fish['base_value']}金币 / 个） \n"
            message += f"\n🐟 总鱼数：{pond_fish['stats']['total_count']} 条\n"
            message += f"💰 总价值：{pond_fish['stats']['total_value']} 金币\n"
            yield event.plain_result(message)
        else:
            yield event.plain_result("🐟 您的鱼塘是空的，快去钓鱼吧！")

    @filter.command("鱼塘容量")
    async def pond_capacity(self, event: AstrMessageEvent):
        """查看用户鱼塘容量"""
        user_id = event.get_sender_id()
        pond_capacity = self.inventory_service.get_user_fish_pond_capacity(user_id)
        if pond_capacity["success"]:
            message = f"🐠 您的鱼塘容量为 {pond_capacity['current_fish_count']} / {pond_capacity['fish_pond_capacity']} 条鱼。"
            yield event.plain_result(message)
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("升级鱼塘", alias={"鱼塘升级"})
    async def upgrade_pond(self, event: AstrMessageEvent):
        """升级鱼塘容量"""
        user_id = event.get_sender_id()
        result = self.inventory_service.upgrade_fish_pond(user_id)
        if result["success"]:
            yield event.plain_result(f"🐠 鱼塘升级成功！新容量为 {result['new_capacity']} 条鱼。")
        else:
            yield event.plain_result(f"❌ 升级失败：{result['message']}")

    @filter.command("鱼竿")
    async def rod(self, event: AstrMessageEvent):
        """查看用户鱼竿信息"""
        user_id = event.get_sender_id()
        rod_info = self.inventory_service.get_user_rod_inventory(user_id)
        if rod_info and rod_info["rods"]:
            # 构造输出信息,附带emoji
            message = "【🎣 鱼竿】：\n"
            for rod in rod_info["rods"]:
                message += format_accessory_or_rod(rod)
                if rod.get("bonus_rare_fish_chance", 1) != 1 and rod.get("bonus_fish_weight", 1.0) != 1.0:
                    message += f"   - 钓上鱼鱼类几率加成: {to_percentage(rod['bonus_rare_fish_chance'])}\n"
                message += f"   -精炼等级: {rod.get('refine_level', 1)}\n"
            
            # 使用通用的长消息处理方法
            async for result in self._send_long_message(event, message, "鱼竿信息"):
                yield result
        else:
            yield event.plain_result("🎣 您还没有鱼竿，快去商店购买或抽奖获得吧！")

    @filter.command("精炼鱼竿", alias={"鱼竿精炼"})
    async def refine_rod(self, event: AstrMessageEvent):
        """精炼鱼竿"""
        user_id = event.get_sender_id()
        rod_info = self.inventory_service.get_user_rod_inventory(user_id)
        if not rod_info or not rod_info["rods"]:
            yield event.plain_result("❌ 您还没有鱼竿，请先购买或抽奖获得。")
            return
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要精炼的鱼竿 ID，例如：/精炼鱼竿 12")
            return
        rod_instance_id = args[1]
        if not rod_instance_id.isdigit():
            yield event.plain_result("❌ 鱼竿 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.refine(user_id, int(rod_instance_id), "rod")
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 精炼鱼竿失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("鱼饵")
    async def bait(self, event: AstrMessageEvent):
        """查看用户鱼饵信息"""
        user_id = event.get_sender_id()
        bait_info = self.inventory_service.get_user_bait_inventory(user_id)
        if bait_info and bait_info["baits"]:
            # 构造输出信息,附带emoji
            message = "【🐟 鱼饵】：\n"
            for bait in bait_info["baits"]:
                message += f" - {bait['name']} x {bait['quantity']} (稀有度: {'⭐' * bait['rarity']})\n"
                message += f"   - ID: {bait['bait_id']}\n"
                if bait["duration_minutes"] > 0:
                    message += f"   - 持续时间: {bait['duration_minutes']} 分钟\n"
                if bait["effect_description"]:
                    message += f"   - 效果: {bait['effect_description']}\n"
                message += "\n"
            yield event.plain_result(message)
        else:
            yield event.plain_result("🐟 您还没有鱼饵，快去商店购买或抽奖获得吧！")

    @filter.command("饰品")
    async def accessories(self, event: AstrMessageEvent):
        """查看用户饰品信息"""
        user_id = event.get_sender_id()
        accessories_info = self.inventory_service.get_user_accessory_inventory(user_id)
        if accessories_info and accessories_info["accessories"]:
            # 构造输出信息,附带emoji
            message = "【💍 饰品】：\n"
            for accessory in accessories_info["accessories"]:
                message += format_accessory_or_rod(accessory)
                message += f"   -精炼等级: {accessory.get('refine_level', 1)}\n"
            
            # 使用通用的长消息处理方法
            async for result in self._send_long_message(event, message, "饰品信息"):
                yield result
        else:
            yield event.plain_result("💍 您还没有饰品，快去商店购买或抽奖获得吧！")

    @filter.command("精炼饰品", alias={"饰品精炼"})
    async def refine_accessory(self, event: AstrMessageEvent):
        """精炼饰品"""
        user_id = event.get_sender_id()
        accessories_info = self.inventory_service.get_user_accessory_inventory(user_id)
        if not accessories_info or not accessories_info["accessories"]:
            yield event.plain_result("❌ 您还没有饰品，请先购买或抽奖获得。")
            return
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要精炼的饰品 ID，例如：/精炼饰品 15")
            return
        accessory_instance_id = args[1]
        if not accessory_instance_id.isdigit():
            yield event.plain_result("❌ 饰品 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.refine(user_id, int(accessory_instance_id), "accessory")
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 精炼饰品失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("使用鱼竿")
    async def use_rod(self, event: AstrMessageEvent):
        """使用鱼竿"""
        user_id = event.get_sender_id()
        rod_info = self.inventory_service.get_user_rod_inventory(user_id)
        if not rod_info or not rod_info["rods"]:
            yield event.plain_result("❌ 您还没有鱼竿，请先购买或抽奖获得。")
            return
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要使用的鱼竿 ID，例如：/使用鱼竿 12")
            return

        rod_instance_id = args[1]
        if not rod_instance_id.isdigit():
            yield event.plain_result("❌ 鱼竿 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.equip_item(user_id, int(rod_instance_id), "rod")
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 使用鱼竿失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("使用鱼饵")
    async def use_bait(self, event: AstrMessageEvent):
        """使用鱼饵"""
        user_id = event.get_sender_id()
        bait_info = self.inventory_service.get_user_bait_inventory(user_id)
        if not bait_info or not bait_info["baits"]:
            yield event.plain_result("❌ 您还没有鱼饵，请先购买或抽奖获得。")
            return
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要使用的鱼饵 ID，例如：/使用鱼饵 13")
            return
        bait_instance_id = args[1]
        if not bait_instance_id.isdigit():
            yield event.plain_result("❌ 鱼饵 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.use_bait(user_id, int(bait_instance_id))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 使用鱼饵失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("使用饰品")
    async def use_accessories(self, event: AstrMessageEvent):
        """使用饰品"""
        user_id = event.get_sender_id()
        accessories_info = self.inventory_service.get_user_accessory_inventory(user_id)
        if not accessories_info or not accessories_info["accessories"]:
            yield event.plain_result("❌ 您还没有饰品，请先购买或抽奖获得。")
            return
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要使用的饰品 ID，例如：/使用饰品 15")
            return
        accessory_instance_id = args[1]
        if not accessory_instance_id.isdigit():
            yield event.plain_result("❌ 饰品 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.equip_item(user_id, int(accessory_instance_id), "accessory")
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 使用饰品失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("金币")
    async def coins(self, event: AstrMessageEvent):
        """查看用户金币信息"""
        user_id = event.get_sender_id()
        user = self.user_repo.get_by_id(user_id)
        if user:
            yield event.plain_result(f"💰 您的金币余额：{user.coins} 金币")
        else:
            yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")

    # ===========商店与市场==========

    @filter.command("全部卖出")
    async def sell_all(self, event: AstrMessageEvent):
        """卖出用户所有鱼"""
        user_id = event.get_sender_id()
        result = self.inventory_service.sell_all_fish(user_id)
        if result:
            yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("保留卖出")
    async def sell_keep(self, event: AstrMessageEvent):
        """卖出用户鱼，但保留每种鱼一条"""
        user_id = event.get_sender_id()
        result = self.inventory_service.sell_all_fish(user_id, keep_one=True)
        if result:
            yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("出售稀有度")
    async def sell_by_rarity(self, event: AstrMessageEvent):
        """按稀有度出售鱼"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要出售的稀有度，例如：/出售稀有度 3")
            return
        rarity = args[1]
        if not rarity.isdigit() or int(rarity) < 1 or int(rarity) > 5:
            yield event.plain_result("❌ 稀有度必须是1到5之间的数字，请检查后重试。")
            return
        result = self.inventory_service.sell_fish_by_rarity(user_id, int(rarity))
        if result:
            yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("出售鱼竿")
    async def sell_rod(self, event: AstrMessageEvent):
        """出售鱼竿"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要出售的鱼竿 ID，例如：/出售鱼竿 12")
            return
        rod_instance_id = args[1]
        if not rod_instance_id.isdigit():
            yield event.plain_result("❌ 鱼竿 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.sell_rod(user_id, int(rod_instance_id))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 出售鱼竿失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    # 批量删除用户鱼竿
    @filter.command("出售所有鱼竿", alias={ "出售全部鱼竿" })
    async def sell_all_rods(self, event: AstrMessageEvent):
        """出售用户所有鱼竿"""
        user_id = event.get_sender_id()
        result = self.inventory_service.sell_all_rods(user_id)
        if result:
            yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("出售饰品")
    async def sell_accessories(self, event: AstrMessageEvent):
        """出售饰品"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要出售的饰品 ID，例如：/出售饰品 15")
            return
        accessory_instance_id = args[1]
        if not accessory_instance_id.isdigit():
            yield event.plain_result("❌ 饰品 ID 必须是数字，请检查后重试。")
            return
        result = self.inventory_service.sell_accessory(user_id, int(accessory_instance_id))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 出售饰品失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("出售所有饰品", alias={ "出售全部饰品" })
    async def sell_all_accessories(self, event: AstrMessageEvent):
        """出售用户所有饰品"""
        user_id = event.get_sender_id()
        result = self.inventory_service.sell_all_accessories(user_id)
        if result:
            yield event.plain_result(result["message"])
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("商店")
    async def shop(self, event: AstrMessageEvent):
        """查看商店"""
        result = self.shop_service.get_shop_listings()
        if result:
            message = "【🛒 商店】\n\n"
            if result["baits"]:
                message += "【🐟 鱼饵】:\n"
                for bait in result["baits"]:
                    message += f" - {bait.name} (ID: {bait.bait_id}) - 价格: {bait.cost} 金币\n - 描述：{bait.description}\n\n"
            else:
                message += "🐟 商店中没有鱼饵可供购买。\n\n"
            if result["rods"]:
                message += "\n【🎣 鱼竿】:\n"
                for rod in result["rods"]:
                    message += f" - {rod.name} (ID: {rod.rod_id}) - 价格: {rod.purchase_cost} 金币\n"
                    if rod.bonus_fish_quality_modifier != 1.0:
                        message += f"   - 质量加成⬆️: {to_percentage(rod.bonus_fish_quality_modifier)}\n"
                    if rod.bonus_fish_quantity_modifier != 1.0:
                        message += f"   - 数量加成⬆️: {to_percentage(rod.bonus_fish_quantity_modifier)}\n"
                    if rod.bonus_rare_fish_chance != 0.0:
                        message += f"   - 钓鱼加成⬆️: {to_percentage(rod.bonus_rare_fish_chance)}\n"
                    message += "\n"
            else:
                message += "🎣 商店中没有鱼竿可供购买。\n"
            yield event.plain_result(message)
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("购买鱼竿")
    async def buy_rod(self, event: AstrMessageEvent):
        """购买鱼竿"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("⚠ 请指定要购买的鱼竿 ID，例如：/购买鱼竿 12")
            return
        rod_instance_id = args[1]
        if not rod_instance_id.isdigit():
            yield event.plain_result("⚠ 鱼竿 ID 必须是数字，请检查后重试。")
            return
        quantity = 1  # 默认购买数量为1
        if len(args) == 3:
            quantity = args[2]
            if not quantity.isdigit() or int(quantity) <= 0:
                yield event.plain_result("⚠ 购买数量必须是正整数，请检查后重试。")
                return
        result = self.shop_service.buy_item(user_id, "rod", int(rod_instance_id), int(quantity))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"⚠ 购买鱼竿失败：{result['message']}")
        else:
            yield event.plain_result("⚠ 出错啦！请稍后再试。")

    @filter.command("购买鱼饵")
    async def buy_bait(self, event: AstrMessageEvent):
        """购买鱼饵"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要购买的鱼饵 ID，例如：/购买鱼饵 13")
            return
        bait_instance_id = args[1]
        if not bait_instance_id.isdigit():
            yield event.plain_result("❌ 鱼饵 ID 必须是数字，请检查后重试。")
            return
        quantity = 1  # 默认购买数量为1
        if len(args) == 3:
            quantity = args[2]
            if not quantity.isdigit() or int(quantity) <= 0:
                yield event.plain_result("❌ 购买数量必须是正整数，请检查后重试。")
                return
        result = self.shop_service.buy_item(user_id, "bait", int(bait_instance_id), int(quantity))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 购买鱼饵失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("市场")
    async def market(self, event: AstrMessageEvent):
        """查看市场"""
        result = self.market_service.get_market_listings()
        if result["success"]:
            message = "【🛒 市场】\n\n"
            if result["rods"]:
                message += "【🎣 鱼竿】:\n"
                for rod in result["rods"]:
                    message += f" - {rod['item_name']} 精{rod['refine_level']} (ID: {rod['market_id']}) - 价格: {rod['price']} 金币\n"
                    message += f" - 售卖人： {rod['seller_nickname']}\n\n"
            else:
                message += "🎣 市场中没有鱼竿可供购买。\n\n"
            if result["accessories"]:
                message += "【💍 饰品】:\n"
                for accessory in result["accessories"]:
                    message += f" - {accessory['item_name']} 精{accessory['refine_level']} (ID: {accessory['market_id']}) - 价格: {accessory['price']} 金币\n"
                    message += f" - 售卖人： {accessory['seller_nickname']}\n\n"
            else:
                message += "💍 市场中没有饰品可供购买。\n"
            yield event.plain_result(message)
        else:
            yield event.plain_result(f"❌ 出错啦！{result['message']}")


    @filter.command("上架鱼竿")
    async def list_rod(self, event: AstrMessageEvent):
        """上架鱼竿到市场"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 3:
            yield event.plain_result("❌ 请指定要上架的鱼竿 ID和价格，例如：/上架鱼竿 12 1000")
            return
        rod_instance_id = args[1]
        if not rod_instance_id.isdigit():
            yield event.plain_result("❌ 鱼竿 ID 必须是数字，请检查后重试。")
            return
        price = args[2]
        if not price.isdigit() or int(price) <= 0:
            yield event.plain_result("❌ 上架价格必须是正整数，请检查后重试。")
            return
        result = self.market_service.put_item_on_sale(user_id, "rod", int(rod_instance_id), int(price))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 上架鱼竿失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("上架饰品")
    async def list_accessories(self, event: AstrMessageEvent):
        """上架饰品到市场"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 3:
            yield event.plain_result("❌ 请指定要上架的饰品 ID和价格，例如：/上架饰品 15 1000")
            return
        accessory_instance_id = args[1]
        if not accessory_instance_id.isdigit():
            yield event.plain_result("❌ 饰品 ID 必须是数字，请检查后重试。")
            return
        price = args[2]
        if not price.isdigit() or int(price) <= 0:
            yield event.plain_result("❌ 上架价格必须是正整数，请检查后重试。")
            return
        result = self.market_service.put_item_on_sale(user_id, "accessory", int(accessory_instance_id), int(price))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 上架饰品失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")
            
    @filter.command("下架")
    async def remove_market_item(self, event: AstrMessageEvent):
        """下架市场上的物品"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要下架的物品 ID，例如：/下架 12")
            return
        
        item_instance_id = args[1]
        if not item_instance_id.isdigit():
            yield event.plain_result("❌ 物品 ID 必须是数字，请检查后重试。")
            return
        
        result = self.market_service.remove_item_from_market(user_id, int(item_instance_id))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 下架失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("我的上架", alias={"我的商品", "上架列表"})
    async def my_market_listings(self, event: AstrMessageEvent):
        """查看自己上架的商品"""
        user_id = event.get_sender_id()
        
        # 获取所有市场商品，然后筛选出用户自己的
        result = self.market_service.get_market_listings()
        if not result["success"]:
            yield event.plain_result(f"❌ 获取市场信息失败：{result.get('message', '未知错误')}")
            return
        
        # 筛选出用户自己的商品
        my_rods = [item for item in result["rods"] if item['user_id'] == user_id]
        my_accessories = [item for item in result["accessories"] if item['user_id'] == user_id]
        
        if not my_rods and not my_accessories:
            yield event.plain_result("📦 您还没有上架任何商品。")
            return
        
        message = "【🛒 我的上架商品】\n\n"
        
        if my_rods:
            message += "【🎣 鱼竿】:\n"
            for rod in my_rods:
                message += f" - {rod['item_name']} 精{rod['refine_level']} (ID: {rod['market_id']})\n"
                message += f"   价格: {rod['price']} 金币\n"
                message += f"   上架时间: {rod['listed_at'].strftime('%Y-%m-%d %H:%M:%S') if hasattr(rod['listed_at'], 'strftime') else rod['listed_at']}\n\n"
        
        if my_accessories:
            message += "【💍 饰品】:\n"
            for accessory in my_accessories:
                message += f" - {accessory['item_name']} 精{accessory['refine_level']} (ID: {accessory['market_id']})\n"
                message += f"   价格: {accessory['price']} 金币\n"
                message += f"   上架时间: {accessory['listed_at'].strftime('%Y-%m-%d %H:%M:%S') if hasattr(accessory['listed_at'], 'strftime') else accessory['listed_at']}\n\n"
        
        message += "💡 使用 `/下架 ID` 命令可以下架指定商品"
        
        yield event.plain_result(message)

    @filter.command("购买")
    async def buy_item(self, event: AstrMessageEvent):
        """购买市场上的物品"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要购买的物品 ID，例如：/购买 12")
            return
        item_instance_id = args[1]
        if not item_instance_id.isdigit():
            yield event.plain_result("❌ 物品 ID 必须是数字，请检查后重试。")
            return
        result = self.market_service.buy_market_item(user_id, int(item_instance_id))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 购买失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")
            
# ===========抽卡与概率玩法==========
    @filter.command("抽卡", alias={"抽奖"})
    async def gacha(self, event: AstrMessageEvent):
        """抽卡"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            # 展示所有的抽奖池信息并显示帮助
            pools = self.gacha_service.get_all_pools()
            if not pools:
                yield event.plain_result("❌ 当前没有可用的抽奖池。")
                return
            message = "【🎰 抽奖池列表】\n\n"
            for pool in pools.get("pools", []):
                message += f"ID: {pool['gacha_pool_id']} - {pool['name']} - {pool['description']}\n 💰 花费：{pool['cost_coins']} 金币 / 次\n\n"
            # 添加卡池详细信息
            message += "【📋 卡池详情】使用「查看卡池 ID」命令查看详细物品概率\n"
            message += "【🎲 抽卡命令】使用「抽卡 ID」命令选择抽卡池进行单次抽卡\n"
            message += "【🎯 十连命令】使用「十连 ID」命令进行十连抽卡\n"
            message += "【🚀 百连命令】使用「百连 ID」命令进行百连抽卡"
            yield event.plain_result(message)
            return
        pool_id = args[1]
        if not pool_id.isdigit():
            yield event.plain_result("❌ 抽奖池 ID 必须是数字，请检查后重试。")
            return
        pool_id = int(pool_id)
        result = self.gacha_service.perform_draw(user_id, pool_id, num_draws=1)
        if result:
            if result["success"]:
                items = result.get("results", [])
                message = f"🎉 抽卡成功！您抽到了 {len(items)} 件物品：\n"
                for item in items:
                    # 构造输出信息
                    if item.get("type") == "coins":
                        # 金币类型的物品
                        message += f"⭐ {item['quantity']} 金币！\n"
                    else:
                        message += f"{'⭐' * item.get('rarity', 1)} {item['name']}\n"
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 抽卡失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    # 修改十连抽卡方法
    @filter.command("十连")
    async def ten_gacha(self, event: AstrMessageEvent):
        """十连抽卡"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要进行十连抽卡的抽奖池 ID，例如：/十连 1")
            return
        pool_id = args[1]
        if not pool_id.isdigit():
            yield event.plain_result("❌ 抽奖池 ID 必须是数字，请检查后重试。")
            return
        pool_id = int(pool_id)
        result = self.gacha_service.perform_draw(user_id, pool_id, num_draws=10)
        if result:
            if result["success"]:
                items = result.get("results", [])
                message = f"🎉 十连抽卡成功！您抽到了 {len(items)} 件物品：\n"
                
                # 统计和分类显示
                kept_items = []
                sold_items_summary = None
                
                for item in items:
                    if item.get("type") == "coins":
                        message += f"⭐ {item['quantity']} 金币！\n"
                    elif item.get("type") == "sold_coins_summary":
                        sold_items_summary = item
                    elif item.get("type", "").startswith("sold_"):
                        # 跳过单个卖出物品的显示，只在汇总中显示
                        continue
                    else:
                        kept_items.append(item)
                        message += f"{'⭐' * item.get('rarity', 1)} {item['name']}\n"
                
                # 显示自动卖出汇总
                if sold_items_summary:
                    message += f"\n💰 自动卖出物品汇总：\n"
                    message += f"   卖出数量：{sold_items_summary['sold_items_count']} 件\n"
                    sold_by_rarity = sold_items_summary['sold_by_rarity']
                    for rarity in [1, 2, 3]:
                        if sold_by_rarity.get(rarity, 0) > 0:
                            message += f"   {'⭐' * rarity}：{sold_by_rarity[rarity]} 件\n"
                    message += f"   获得金币：{sold_items_summary['quantity']} 💰\n"
                    message += f"\n📝 四星以下物品已自动卖出换取金币"
                
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 抽卡失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("百连")
    async def hundred_gacha(self, event: AstrMessageEvent):
        """百连抽卡 - 使用内存聚合优化，支持大批量抽奖，自动卖出四星以下物品"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要进行百连抽卡的抽奖池 ID，例如：/百连 1")
            return
        pool_id = args[1]
        if not pool_id.isdigit():
            yield event.plain_result("❌ 抽奖池 ID 必须是数字，请检查后重试。")
            return
        
        pool_id = int(pool_id)
        
        # 添加确认提示，因为百连成本较高
        pool_info = self.gacha_service.gacha_repo.get_pool_by_id(pool_id)
        if not pool_info:
            yield event.plain_result("❌ 指定的抽奖池不存在。")
            return
            
        total_cost = pool_info.cost_coins * 100
        user = self.user_repo.get_by_id(user_id)
        if not user:
            yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")
            return
            
        if not user.can_afford(total_cost):
            yield event.plain_result(f"💰 金币不足！百连需要 {total_cost} 金币，您当前拥有 {user.coins} 金币。")
            return
        
        # 提示用户即将进行的操作
        yield event.plain_result(f"🚀 正在进行百连抽卡，预计花费 {total_cost} 金币...\n⏳ 请稍等，正在处理...\n📝 四星以下物品将自动卖出换取金币")
        
        result = self.gacha_service.perform_draw(user_id, pool_id, num_draws=100)
        if result:
            if result["success"]:
                items = result.get("results", [])
                
                # 统计结果
                rarity_count = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                coins_total = 0
                special_items = []
                sold_items_summary = None
                
                for item in items:
                    if item.get("type") == "coins":
                        coins_total += item['quantity']
                    elif item.get("type") == "sold_coins_summary":
                        sold_items_summary = item
                    elif item.get("type", "").startswith("sold_"):
                        # 自动卖出的物品不显示在特殊物品中
                        continue
                    else:
                        rarity = item.get('rarity', 1)
                        rarity_count[rarity] += 1
                        
                        # 收集4星及以上的特殊物品
                        if rarity >= 4:
                            special_items.append(f"{'⭐' * rarity} {item['name']}")
                
                # 构建消息
                message = f"🎊 百连抽卡完成！\n\n"
                
                # 稀有度统计（只显示保留的物品）
                message += "📊 【保留物品统计】\n"
                kept_items_count = 0
                for rarity in range(5, 0, -1):
                    if rarity_count[rarity] > 0:
                        message += f"{'⭐' * rarity}：{rarity_count[rarity]} 件\n"
                        kept_items_count += rarity_count[rarity]
                
                if coins_total > 0:
                    normal_coins = coins_total
                    if sold_items_summary:
                        normal_coins = coins_total - sold_items_summary['quantity']
                    if normal_coins > 0:
                        message += f"💰 直接金币：{normal_coins}\n"
                
                # 显示自动卖出汇总
                if sold_items_summary:
                    message += f"\n💸 【自动卖出汇总】\n"
                    message += f"卖出数量：{sold_items_summary['sold_items_count']} 件\n"
                    sold_by_rarity = sold_items_summary['sold_by_rarity']
                    for rarity in [1, 2, 3]:
                        if sold_by_rarity.get(rarity, 0) > 0:
                            message += f"{'⭐' * rarity}：{sold_by_rarity[rarity]} 件\n"
                    message += f"获得金币：{sold_items_summary['quantity']} 💰\n"
                
                # 显示4星及以上物品
                if special_items:
                    message += f"\n🌟 【珍稀物品】\n"
                    for item in special_items[:10]:  # 最多显示10个
                        message += f"{item}\n"
                    if len(special_items) > 10:
                        message += f"...还有{len(special_items)-10}件珍稀物品\n"
                
                total_items = kept_items_count + (sold_items_summary['sold_items_count'] if sold_items_summary else 0)
                message += f"\n📋 总计：保留 {kept_items_count} 件，卖出 {sold_items_summary['sold_items_count'] if sold_items_summary else 0} 件"
                
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 抽卡失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")
        
    @filter.command("千连")
    async def thousand_gacha(self, event: AstrMessageEvent):
        """千连抽卡 - 大批量抽奖，自动卖出四星以下物品"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要进行千连抽卡的抽奖池 ID，例如：/千连 1")
            return
        pool_id = args[1]
        if not pool_id.isdigit():
            yield event.plain_result("❌ 抽奖池 ID 必须是数字，请检查后重试。")
            return
        
        pool_id = int(pool_id)
        
        # 获取抽奖池信息并验证费用
        pool_info = self.gacha_service.gacha_repo.get_pool_by_id(pool_id)
        if not pool_info:
            yield event.plain_result("❌ 指定的抽奖池不存在。")
            return
            
        total_cost = pool_info.cost_coins * 1000
        user = self.user_repo.get_by_id(user_id)
        if not user:
            yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")
            return
            
        if not user.can_afford(total_cost):
            yield event.plain_result(f"💰 金币不足！千连需要 {total_cost:,} 金币，您当前拥有 {user.coins:,} 金币。")
            return
        
        # 提示用户即将进行的操作
        yield event.plain_result(f"🚀 正在进行千连抽卡，预计花费 {total_cost:,} 金币...\n⏳ 请稍等，正在处理...\n📝 四星以下物品将自动卖出换取金币")
        
        result = self.gacha_service.perform_draw(user_id, pool_id, num_draws=1000)
        if result:
            if result["success"]:
                items = result.get("results", [])
                
                # 统计结果
                rarity_count = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                coins_total = 0
                special_items = []  # 4星物品
                ultra_rare_items = []  # 5星物品
                sold_items_summary = None
                
                for item in items:
                    if item.get("type") == "coins":
                        coins_total += item['quantity']
                    elif item.get("type") == "sold_coins_summary":
                        sold_items_summary = item
                    elif item.get("type", "").startswith("sold_"):
                        # 自动卖出的物品不显示在特殊物品中
                        continue
                    else:
                        rarity = item.get('rarity', 1)
                        rarity_count[rarity] += 1
                        
                        # 收集特殊物品
                        if rarity == 5:
                            ultra_rare_items.append(f"⭐⭐⭐⭐⭐ {item['name']}")
                        elif rarity == 4:
                            special_items.append(f"⭐⭐⭐⭐ {item['name']}")
                
                # 构建消息
                message = f"🎊 千连抽卡完成！\n\n"
                
                # 稀有度统计（只显示保留的物品）
                message += "📊 【保留物品统计】\n"
                kept_items_count = 0
                for rarity in range(5, 0, -1):
                    if rarity_count[rarity] > 0:
                        percentage = (rarity_count[rarity] / 1000) * 100
                        message += f"{'⭐' * rarity}：{rarity_count[rarity]:,} 件 ({percentage:.1f}%)\n"
                        kept_items_count += rarity_count[rarity]
                
                if coins_total > 0:
                    normal_coins = coins_total
                    if sold_items_summary:
                        normal_coins = coins_total - sold_items_summary['quantity']
                    if normal_coins > 0:
                        message += f"💰 直接金币：{normal_coins:,}\n"
                
                # 显示自动卖出汇总
                if sold_items_summary:
                    message += f"\n💸 【自动卖出汇总】\n"
                    message += f"卖出数量：{sold_items_summary['sold_items_count']:,} 件\n"
                    sold_by_rarity = sold_items_summary['sold_by_rarity']
                    for rarity in [1, 2, 3]:
                        if sold_by_rarity.get(rarity, 0) > 0:
                            percentage = (sold_by_rarity[rarity] / 1000) * 100
                            message += f"{'⭐' * rarity}：{sold_by_rarity[rarity]:,} 件 ({percentage:.1f}%)\n"
                    message += f"获得金币：{sold_items_summary['quantity']:,} 💰\n"
                
                # 显示5星物品（最珍贵的）
                if ultra_rare_items:
                    message += f"\n🌟 【传说物品 ({len(ultra_rare_items)}件)】\n"
                    for item in ultra_rare_items[:10]:  # 最多显示10个5星
                        message += f"{item}\n"
                    if len(ultra_rare_items) > 10:
                        message += f"...还有{len(ultra_rare_items)-10}件传说物品\n"
                
                # 显示4星物品
                if special_items:
                    message += f"\n⭐⭐⭐⭐ 【稀有物品 ({len(special_items)}件)】\n"
                    if len(special_items) <= 15:  # 少于15件时详细显示
                        for item in special_items:
                            message += f"{item}\n"
                    else:
                        # 只显示前10件
                        for item in special_items[:10]:
                            message += f"{item}\n"
                        message += f"...还有{len(special_items)-10}件稀有物品\n"
                
                # 总结
                total_kept = kept_items_count
                total_sold = sold_items_summary['sold_items_count'] if sold_items_summary else 0
                message += f"\n📋 总计：保留 {total_kept:,} 件，卖出 {total_sold:,} 件"
                
                # 投资回报分析
                if sold_items_summary:
                    total_return = coins_total
                    roi_percentage = ((total_return - total_cost) / total_cost) * 100
                    # if roi_percentage > 0:
                    #     message += f"\n📈 投资回报：+{roi_percentage:.1f}% (盈利 {total_return - total_cost:,} 金币)"
                    # else:
                    #     message += f"\n📉 投资回报：{roi_percentage:.1f}% (亏损 {abs(total_return - total_cost):,} 金币)"
                
                yield event.plain_result(message)
                
                # 如果5星物品较多，发送详细列表
                if len(ultra_rare_items) > 10:
                    detail_message = f"🌟 【完整传说物品列表】({len(ultra_rare_items)}件)\n"
                    for i, item in enumerate(ultra_rare_items, 1):
                        detail_message += f"{i}. {item}\n"
                    
                    # 使用长消息处理方法
                    async for result in self._send_long_message(event, detail_message, "千连传说物品详情"):
                        yield result
                
            else:
                yield event.plain_result(f"❌ 抽卡失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")    
    
    @filter.command("万连")
    async def ten_thousand_gacha(self, event: AstrMessageEvent):
        """万连抽卡 - 终极批量抽奖，全面测试系统性能"""
        user_id = event.get_sender_id()
        
        # 检查万连锁
        if self.ten_thousand_gacha_lock:
            current_user = self.ten_thousand_gacha_user
            if current_user == user_id:
                yield event.plain_result("❌ 您已经在进行万连，请等待当前万连完成")
            else:
                user = self.user_repo.get_by_id(current_user) if current_user else None
                nickname = user.nickname if user else "未知用户"
                yield event.plain_result(f"⏳ 系统正忙：{nickname} 正在进行万连\n"
                                    f"为保证系统稳定，同一时间只能有一人万连\n"
                                    f"请稍后再试")
            return
        
        args = event.message_str.split(" ")
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要进行万连抽卡的抽奖池 ID，例如：/万连 1")
            return
        pool_id = args[1]
        if not pool_id.isdigit():
            yield event.plain_result("❌ 抽奖池 ID 必须是数字，请检查后重试。")
            return
        
        pool_id = int(pool_id)
        
        # 获取抽奖池信息
        pool_info = self.gacha_service.gacha_repo.get_pool_by_id(pool_id)
        if not pool_info:
            yield event.plain_result("❌ 指定的抽奖池不存在。")
            return
            
        total_cost = pool_info.cost_coins * 10000
        user = self.user_repo.get_by_id(user_id)
        if not user:
            yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")
            return
            
        # 万连的金币要求很高，给出详细的提示
        if not user.can_afford(total_cost):
            yield event.plain_result(f"💰 金币严重不足！\n"
                                    f"万连需要：{total_cost:,} 金币\n"
                                    f"您当前拥有：{user.coins:,} 金币\n"
                                    f"还需要：{total_cost - user.coins:,} 金币")
            return
        
        # 万连风险提示和二次确认
        yield event.plain_result(f"⚠️ 【万连抽卡风险提示】\n"
                                f"💸 花费：{total_cost:,} 金币\n"
                                f"⏱️ 预计处理时间：10-30秒\n"
                                f"📦 将获得大量物品，四星以下自动卖出\n"
                                f"🔄 正在启动万连系统...")
        
        # 开始计时
        import time
        start_time = time.time()
        
        # 分批处理以避免内存问题（可选，如果单次10000有问题的话）
        batch_size = 2000  # 每批2000次
        total_results = []
        processed = 0
        
        try:
            # 设置万连锁
            self.ten_thousand_gacha_lock = True
            self.ten_thousand_gacha_user = user_id
            logger.info(f"用户 {user_id} 开始万连，已设置系统锁")
            
            
            # 分5批处理
            failed_at_batch = -1
            for batch in range(5):
                batch_result = self.gacha_service.perform_draw(user_id, pool_id, num_draws=batch_size)
                if batch_result and batch_result["success"]:
                    total_results.extend(batch_result.get("results", []))
                    processed += batch_size
                    
                    # 进度提示
                    if batch < 4:  # 不在最后一批显示进度
                        progress = ((batch + 1) / 5) * 100
                        yield event.plain_result(f"🔄 处理进度：{progress:.0f}% ({processed:,}/10,000)")
                else:
                    # 记录失败的批次
                    failed_at_batch = batch + 1
                    # 如果某批失败，退还剩余费用
                    remaining_batches = 5 - batch
                    remaining_cost = remaining_batches * batch_size * pool_info.cost_coins
                    if remaining_cost > 0:
                        user.coins += remaining_cost
                        self.user_repo.update(user)
                    break  # 跳出循环，但继续处理已完成的结果
            
            # 处理完成，统计结果
            end_time = time.time()
            process_time = end_time - start_time
            
            # 判断是否完全完成
            is_partial = failed_at_batch > 0
            actual_draws = processed
            actual_cost = actual_draws * pool_info.cost_coins
            refunded_cost = total_cost - actual_cost if is_partial else 0
            
            # 详细统计
            rarity_count = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            coins_from_draws = 0
            special_items = []
            ultra_rare_items = []  # 5星物品单独统计
            sold_items_total = 0
            sold_coins_total = 0
            sold_by_rarity = {1: 0, 2: 0, 3: 0}
            
            for item in total_results:
                if item.get("type") == "coins":
                    coins_from_draws += item['quantity']
                elif item.get("type") == "sold_coins_summary":
                    sold_items_total += item['sold_items_count']
                    sold_coins_total += item['quantity']
                    for rarity in [1, 2, 3]:
                        sold_by_rarity[rarity] += item['sold_by_rarity'].get(rarity, 0)
                elif item.get("type", "").startswith("sold_"):
                    continue
                else:
                    rarity = item.get('rarity', 1)
                    rarity_count[rarity] += 1
                    
                    # 收集特殊物品
                    if rarity == 5:
                        ultra_rare_items.append(f"⭐⭐⭐⭐⭐ {item['name']}")
                    elif rarity == 4:
                        special_items.append(f"⭐⭐⭐⭐ {item['name']}")
            
            # 构建详细结果消息
            if is_partial:
                message = f"⚠️ 万连部分完成（第{failed_at_batch}批出错）\n"
                message += f"✅ 已完成：{actual_draws:,}/10,000 抽\n"
                message += f"💰 已退还：{refunded_cost:,} 金币\n"
            else:
                message = f"🎊 万连抽卡完成！\n"
            
            message += f"⏱️ 处理时间：{process_time:.1f}秒\n\n"
            
            # 核心统计
            kept_items = sum(rarity_count.values())
            message += f"📊 【核心统计】\n"
            message += f"🎯 实际抽数：{actual_draws:,} 抽\n"
            message += f"💎 保留物品：{kept_items:,} 件\n"
            message += f"💸 卖出物品：{sold_items_total:,} 件\n"
            message += f"💰 总获得金币：{coins_from_draws + sold_coins_total:,}\n"
            if is_partial:
                message += f"💵 实际花费：{actual_cost:,} 金币\n"
            message += "\n"
            
            # 保留物品详情（基于实际抽奖数计算百分比）
            if kept_items > 0:
                message += f"🏆 【保留物品详情】\n"
                for rarity in range(5, 0, -1):
                    if rarity_count[rarity] > 0:
                        percentage = (rarity_count[rarity] / actual_draws) * 100
                        message += f"{'⭐' * rarity}：{rarity_count[rarity]:,} 件 ({percentage:.2f}%)\n"
            
            # 卖出汇总
            if sold_items_total > 0:
                message += f"\n💸 【自动卖出详情】\n"
                total_sold_percentage = (sold_items_total / actual_draws) * 100
                message += f"卖出总数：{sold_items_total:,} 件 ({total_sold_percentage:.1f}%)\n"
                for rarity in [1, 2, 3]:
                    if sold_by_rarity[rarity] > 0:
                        percentage = (sold_by_rarity[rarity] / actual_draws) * 100
                        message += f"{'⭐' * rarity}：{sold_by_rarity[rarity]:,} 件 ({percentage:.2f}%)\n"
                message += f"获得金币：{sold_coins_total:,} 💰\n"
            
            # 5星物品展示（最珍贵的）
            if ultra_rare_items:
                message += f"\n🌟 【传说物品 ({len(ultra_rare_items)}件)】\n"
                for item in ultra_rare_items[:15]:  # 最多显示15个5星
                    message += f"{item}\n"
                if len(ultra_rare_items) > 15:
                    message += f"...还有{len(ultra_rare_items)-15}件传说物品\n"
            
            # 4星物品概览（数量较多时只显示总数）
            if rarity_count[4] > 0:
                message += f"\n⭐⭐⭐⭐ 【稀有物品】：{rarity_count[4]:,} 件\n"
                if rarity_count[4] <= 20:  # 少于20件时详细显示
                    for item in special_items[:rarity_count[4]]:
                        message += f"{item}\n"
            
            # 性能统计
            # message += f"\n📈 【性能统计】\n"
            # message += f"处理速度：{actual_draws/process_time:.0f} 抽/秒\n"
            # message += f"命中率：保留{(kept_items/actual_draws)*100:.1f}% | 卖出{(sold_items_total/actual_draws)*100:.1f}%\n"
            
            # 投资回报分析
            # if sold_coins_total > 0:
            #     total_return = coins_from_draws + sold_coins_total
            #     roi_percentage = ((total_return - actual_cost) / actual_cost) * 100
            #     message += f"📊 投资回报：{roi_percentage:+.1f}% ({total_return:,} vs {actual_cost:,})\n"
            
            # 如果是部分完成，添加提醒信息
            if is_partial:
                message += f"\n⚠️ 【重要提醒】\n"
                message += f"由于第{failed_at_batch}批处理出错，万连提前结束\n"
                message += f"已退还剩余 {refunded_cost:,} 金币到您的账户\n"
                message += f"您可以稍后重试或联系管理员查看问题\n"
            
            yield event.plain_result(message)
            
            # 释放万连锁
            self.ten_thousand_gacha_lock = False
            self.ten_thousand_gacha_user = None
            logger.info(f"用户 {user_id} 万连完成，已释放系统锁")
            
            # 如果有很多5星物品，单独发送详细列表（只在有结果时发送）
            if len(ultra_rare_items) > 15:
                detail_message = f"🌟 【完整传说物品列表】({len(ultra_rare_items)}件)\n"
                for i, item in enumerate(ultra_rare_items, 1):
                    detail_message += f"{i}. {item}\n"
                    if i % 20 == 0 and i < len(ultra_rare_items):
                        detail_message += f"\n--- 第{i//20}批 ---\n"
                
                # 如果列表太长，使用转发消息
                if len(detail_message) > 1000:
                    async for result in self._send_long_message(event, detail_message, "万连传说物品详情"):
                        yield result
                else:
                    yield event.plain_result(detail_message)
            
        except Exception as e:
            logger.error(f"万连抽卡出错: {e}", exc_info=True)
            # 出错时尝试退还费用，但要考虑可能已经处理了部分批次
            try:
                # 如果还没开始处理或者在第一批就出错，全额退还
                if processed == 0:
                    user = self.user_repo.get_by_id(user_id)
                    user.coins += total_cost  # 全额退还
                    self.user_repo.update(user)
                    yield event.plain_result(f"❌ 万连启动失败，已全额退还 {total_cost:,} 金币：{str(e)}")
                else:
                    # 如果已经处理了部分，只退还剩余部分
                    actual_cost = processed * pool_info.cost_coins
                    remaining_cost = total_cost - actual_cost
                    
                    if remaining_cost > 0:
                        user = self.user_repo.get_by_id(user_id)
                        user.coins += remaining_cost
                        self.user_repo.update(user)
                    
                    # 显示部分完成的统计（简化版）
                    if total_results:
                        kept_count = 0
                        sold_count = 0
                        sold_coins = 0
                        for item in total_results:
                            if item.get("type") == "sold_coins_summary":
                                sold_count += item['sold_items_count']
                                sold_coins += item['quantity']
                            elif not item.get("type", "").startswith("sold_"):
                                kept_count += 1
                        
                        yield event.plain_result(f"⚠️ 万连异常中止，但已完成 {processed:,} 抽\n"
                                            f"📦 获得物品：{kept_count} 件保留，{sold_count} 件卖出\n"
                                            f"💰 获得金币：{sold_coins:,}\n"
                                            f"💵 已退还剩余费用：{remaining_cost:,} 金币\n"
                                            f"❌ 错误信息：{str(e)}")
                    else:
                        yield event.plain_result(f"❌ 万连在处理 {processed:,} 抽后出错\n"
                                            f"💵 已退还剩余 {remaining_cost:,} 金币\n"
                                            f"请联系管理员：{str(e)}")
            
            except Exception as refund_error:
                yield event.plain_result(f"❌ 万连处理出错且退款失败！\n"
                                    f"已处理：{processed:,} 抽\n"
                                    f"原错误：{str(e)}\n"
                                    f"退款错误：{str(refund_error)}\n"
                                    f"请立即联系管理员处理")
        finally:
            # 确保无论如何都释放锁
            self.ten_thousand_gacha_lock = False
            self.ten_thousand_gacha_user = None
            logger.info(f"用户 {user_id} 万连异常结束，已释放系统锁")

    @filter.command("查看卡池")
    async def view_gacha_pool(self, event: AstrMessageEvent):
        """查看当前卡池"""
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要查看的卡池 ID，例如：/查看卡池 1")
            return
        pool_id = args[1]
        if not pool_id.isdigit():
            yield event.plain_result("❌ 卡池 ID 必须是数字，请检查后重试。")
            return
        pool_id = int(pool_id)
        result = self.gacha_service.get_pool_details(pool_id)
        if result:
            if result["success"]:
                pool = result.get("pool", {})
                message = "【🎰 卡池详情】\n\n"
                message += f"ID: {pool['gacha_pool_id']} - {pool['name']}\n"
                message += f"描述: {pool['description']}\n"
                message += f"花费: {pool['cost_coins']} 金币 / 次\n"
                message += f"十连花费: {pool['cost_coins'] * 10} 金币\n"
                message += f"百连花费: {pool['cost_coins'] * 100} 金币\n\n"
                message += "【📋 物品概率】\n"

                if result["probabilities"]:
                    for item in result["probabilities"]:
                        # 直接计算概率显示，支持更高精度
                        probability = item['probability']
                        if probability >= 1.0:
                            percentage = (probability - 1.0) * 100
                        else:
                            percentage = probability * 100
                        probability_str = f"{percentage:.8f}%"
                        
                        message += f" - {'⭐' * item.get('item_rarity', 0)} {item['item_name']} (概率: {probability_str})\n"
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 查看卡池失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("抽卡记录")
    async def gacha_history(self, event: AstrMessageEvent):
        """查看抽卡记录"""
        user_id = event.get_sender_id()
        result = self.gacha_service.get_user_gacha_history(user_id)
        if result:
            if result["success"]:
                history = result.get("records", [])
                if not history:
                    yield event.plain_result("📜 您还没有抽卡记录。")
                    return
                message = "【📜 抽卡记录】\n\n"
                for record in history:
                    message += f"物品名称: {record['item_name']} (稀有度: {'⭐' * record['rarity']})\n"
                    message += f"时间: {safe_datetime_handler(record['timestamp'])}\n\n"
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 查看抽卡记录失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("擦弹")
    async def wipe_bomb(self, event: AstrMessageEvent):
        """擦弹功能"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("💸 请指定要擦弹的数量 ID，例如：/擦弹 123456789")
            return
        contribution_amount = args[1]
        if contribution_amount in ['allin', 'halfin', '梭哈', '梭一半']:
            # 查询用户当前金币数量
            user = self.user_repo.get_by_id(user_id)
            if user:
                coins = user.coins
            else:
                yield event.plain_result("❌ 您还没有注册，请先使用 /注册 命令注册。")
                return
            if contribution_amount == 'allin' or contribution_amount == '梭哈':
                contribution_amount = coins
            elif contribution_amount == 'halfin' or contribution_amount == '梭一半':
                contribution_amount = coins // 2
            contribution_amount = str(contribution_amount)
        # 判断是否为int或数字字符串
        if not contribution_amount.isdigit():
            yield event.plain_result("❌ 擦弹数量必须是数字，请检查后重试。")
            return
        result = self.game_mechanics_service.perform_wipe_bomb(user_id, int(contribution_amount))
        if result:
            if result["success"]:
                message = ""
                contribution = result["contribution"]
                multiplier = result["multiplier"]
                reward = result["reward"]
                profit = result["profit"]
                remaining_today = result["remaining_today"]
                sunshine_triggered = result.get("sunshine_triggered", False)
                sunshine_amount = result.get("sunshine_amount", 0)
                
                if multiplier >= 3:
                    message += f"🎰 大成功！你投入 {contribution} 金币，获得了 {multiplier} 倍奖励！\n 💰 奖励金额：{reward} 金币（盈利：+ {profit}）\n"
                elif multiplier >= 1:
                    message += f"🎲 你投入 {contribution} 金币，获得了 {multiplier} 倍奖励！\n 💰 奖励金额：{reward} 金币（盈利：+ {profit}）\n"
                else:
                    message += f"💥 你投入 {contribution} 金币，获得了 {multiplier} 倍奖励！\n 💰 奖励金额：{reward} 金币（亏损：- {abs(profit - sunshine_amount)})\n"
                
                # 阳光普照提示
                if sunshine_triggered:
                    message += f"☀️ 阳光普照！系统返还了 {sunshine_amount} 金币作为安慰奖！\n"
                    
                message += f"剩余擦弹次数：{remaining_today} 次\n"
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"⚠️ 擦弹失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("擦弹记录", alias={"擦弹历史"})
    async def wipe_bomb_history(self, event: AstrMessageEvent):
        """查看擦弹记录"""
        user_id = event.get_sender_id()
        result = self.game_mechanics_service.get_wipe_bomb_history(user_id)
        if result:
            if result["success"]:
                history = result.get("logs", [])
                if not history:
                    yield event.plain_result("📜 您还没有擦弹记录。")
                    return
                message = "【📜 擦弹记录】\n\n"
                for record in history:
                    # 添加一点emoji
                    message += f"⏱️ 时间: {safe_datetime_handler(record['timestamp'])}\n"
                    message += f"💸 投入: {record['contribution']} 金币, 🎁 奖励: {record['reward']} 金币\n"
                    # 计算盈亏
                    profit = record["reward"] - record["contribution"]
                    profit_text = f"盈利: +{profit}" if profit >= 0 else f"亏损: {profit}"
                    profit_emoji = "📈" if profit >= 0 else "📉"

                    if record["multiplier"] >= 3:
                        message += f"🔥 倍率: {record['multiplier']} ({profit_emoji} {profit_text})\n\n"
                    elif record["multiplier"] >= 1:
                        message += f"✨ 倍率: {record['multiplier']} ({profit_emoji} {profit_text})\n\n"
                    else:
                        message += f"💔 倍率: {record['multiplier']} ({profit_emoji} {profit_text})\n\n"
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 查看擦弹记录失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    # ===========社交==========
    @filter.command("排行榜", alias={"phb"})
    async def ranking(self, event: AstrMessageEvent):
        """查看排行榜"""
        user_data = self.user_service.get_leaderboard_data().get("leaderboard", [])
        if not user_data:
            yield event.plain_result("❌ 当前没有排行榜数据。")
            return
        for user in user_data:
            if user["title"] is None:
                user["title"] = "无称号"
            if user["accessory"] is None:
                user["accessory"] = "无饰品"
            if user["fishing_rod"] is None:
                user["fishing_rod"] = "无鱼竿"
        # logger.info(f"用户数据: {user_data}")
        draw_fishing_ranking(user_data, output_path="fishing_ranking.png")
        yield event.image_result("fishing_ranking.png")

    @filter.command("偷鱼")
    async def steal_fish(self, event: AstrMessageEvent):
        """偷鱼功能"""
        user_id = event.get_sender_id()
        message_obj = event.message_obj
        target_id = None
        if hasattr(message_obj, "message"):
            # 检查消息中是否有At对象
            for comp in message_obj.message:
                if isinstance(comp, At):
                    target_id = comp.qq
                    break
        if target_id is None:
            yield event.plain_result("请在消息中@要偷鱼的用户")
            return
        if int(target_id) == int(user_id):
            yield event.plain_result("不能偷自己的鱼哦！")
            return
        result = self.game_mechanics_service.steal_fish(user_id, target_id)
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 偷鱼失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("查看称号", alias={"称号"})
    async def view_titles(self, event: AstrMessageEvent):
        """查看用户称号"""
        user_id = event.get_sender_id()
        titles = self.user_service.get_user_titles(user_id).get("titles", [])
        if titles:
            message = "【🏅 您的称号】\n"
            for title in titles:
                message += f"- {title['name']} (ID: {title['title_id']})\n- 描述: {title['description']}\n\n"
            yield event.plain_result(message)
        else:
            yield event.plain_result("❌ 您还没有任何称号，快去完成成就或参与活动获取吧！")


    @filter.command("使用称号")
    async def use_title(self, event: AstrMessageEvent):
        """使用称号"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            yield event.plain_result("❌ 请指定要使用的称号 ID，例如：/使用称号 1")
            return
        title_id = args[1]
        if not title_id.isdigit():
            yield event.plain_result("❌ 称号 ID 必须是数字，请检查后重试。")
            return
        result = self.user_service.use_title(user_id, int(title_id))
        if result:
            if result["success"]:
                yield event.plain_result(result["message"])
            else:
                yield event.plain_result(f"❌ 使用称号失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("查看成就", alias={ "成就" })
    async def view_achievements(self, event: AstrMessageEvent):
        """查看用户成就"""
        user_id = event.get_sender_id()
        achievements = self.achievement_service.get_user_achievements(user_id).get("achievements", [])
        if achievements:
            message = "【🏆 您的成就】\n"
            for achievement in achievements:
                message += f"- {achievement['name']} (ID: {achievement['id']})\n"
                message += f"  描述: {achievement['description']}\n"
                if achievement.get("completed_at"):
                    message += f"  完成时间: {safe_datetime_handler(achievement['completed_at'])}\n"
                else:
                    message += "  进度: {}/{}\n".format(achievement["progress"], achievement["target"])
            message += "请继续努力完成更多成就！"
            yield event.plain_result(message)
        else:
            yield event.plain_result("❌ 您还没有任何成就，快去完成任务或参与活动获取吧！")

    @filter.command("税收记录")
    async def tax_record(self, event: AstrMessageEvent):
        """查看税收记录"""
        user_id = event.get_sender_id()
        result = self.user_service.get_tax_record(user_id)
        if result:
            if result["success"]:
                records = result.get("records", [])
                if not records:
                    yield event.plain_result("📜 您还没有税收记录。")
                    return
                message = "【📜 税收记录】\n\n"
                for record in records:
                    message += f"⏱️ 时间: {safe_datetime_handler(record['timestamp'])}\n"
                    message += f"💰 金额: {record['amount']} 金币\n"
                    message += f"📊 描述: {record['tax_type']}\n\n"
                yield event.plain_result(message)
            else:
                yield event.plain_result(f"❌ 查看税收记录失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.command("钓鱼区域", alias={"区域"})
    async def fishing_area(self, event: AstrMessageEvent):
        """查看当前钓鱼区域"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 2:
            result = self.fishing_service.get_user_fishing_zones(user_id)
            if result:
                if result["success"]:
                    zones = result.get("zones", [])
                    message = f"【🌊 钓鱼区域】\n"
                    for zone in zones:
                        message += f"区域名称: {zone['name']} (ID: {zone['zone_id']}) {'✅' if zone['whether_in_use'] else ''}\n"
                        message += f"描述: {zone['description']}\n"
                        if zone['zone_id'] >= 2:
                            message += f"剩余稀有鱼类数量: {zone['daily_rare_fish_quota'] - zone['rare_fish_caught_today']}）\n"
                    message += "使用「/钓鱼区域 ID」命令切换钓鱼区域。\n"
                    yield event.plain_result(message)
                else:
                    yield event.plain_result(f"❌ 查看钓鱼区域失败：{result['message']}")
            else:
                yield event.plain_result("❌ 出错啦！请稍后再试。")
            return
        zone_id = args[1]
        if not zone_id.isdigit():
            yield event.plain_result("❌ 钓鱼区域 ID 必须是数字，请检查后重试。")
            return
        zone_id = int(zone_id)
        if zone_id not in [1, 2, 3]:
            yield event.plain_result("❌ 钓鱼区域 ID 必须是 1、2 或 3，请检查后重试。")
            return
        # 切换用户的钓鱼区域
        result = self.fishing_service.set_user_fishing_zone(user_id, zone_id)
        yield event.plain_result(result["message"] if result else "❌ 出错啦！请稍后再试。")

    @filter.command("钓鱼帮助", alias={"钓鱼菜单", "菜单"})
    async def fishing_help(self, event: AstrMessageEvent):
        """显示钓鱼插件帮助信息"""
        image = draw_help_image()
        yield event.image_result(image)

    @filter.command("鱼类图鉴")
    async def fish_pokedex(self, event: AstrMessageEvent):
        """查看鱼类图鉴"""
        user_id = event.get_sender_id()
        result = self.fishing_service.get_user_pokedex(user_id)

        if result:
            if result["success"]:
                pokedex = result.get("pokedex", [])
                if not pokedex:
                    yield event.plain_result("❌ 您还没有捕捉到任何鱼类，快去钓鱼吧！")
                    return

                message = "【🐟 🌊 鱼类图鉴 📖 🎣】\n"
                message += f"🏆 解锁进度：{to_percentage(1.0 + result['unlocked_percentage'])}\n"
                message += f"📊 收集情况：{result['unlocked_fish_count']} / {result['total_fish_count']} 种\n"

                for fish in pokedex:
                    rarity = fish["rarity"]

                    message += f" - {fish['name']} ({'✨' * rarity})\n"
                    message += f"💎 价值：{fish['value']} 金币\n"
                    message += f"🕰️ 首次捕获：{safe_datetime_handler(fish['first_caught_time'])}\n"
                    message += f"📜 描述：{fish['description']}\n"

                if len(message) <= 500:
                    yield event.plain_result(message)
                    return

                text_chunk_size = 1000  # 每个Plain文本块的最大字数
                node_chunk_size = 4  # 每个Node中最多包含的Plain文本块数量
                text_chunks = [message[i:i + text_chunk_size] for i in
                               range(0, len(message), text_chunk_size)]

                if not text_chunks:
                    yield event.plain_result("❌ 内容为空，无法发送。")
                    return

                grouped_chunks = [text_chunks[i:i + node_chunk_size] for i in
                                  range(0, len(text_chunks), node_chunk_size)]

                from astrbot.api.message_components import Node, Plain
                nodes_to_send = []
                for i, group in enumerate(grouped_chunks):
                    plain_components = [Plain(text=chunk) for chunk in group]

                    node = Node(
                        uin=event.get_self_id(),
                        name=f"鱼类图鉴 - 第 {i + 1} 页",
                        content=plain_components
                    )
                    nodes_to_send.append(node)

                try:
                    yield event.chain_result(nodes_to_send)
                except Exception as e:
                    yield event.plain_result(f"❌ 发送转发消息失败：{e}")

            else:
                yield event.plain_result(f"❌ 查看鱼类图鉴失败：{result['message']}")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")
    # ===========管理后台==========

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("修改金币")
    async def modify_coins(self, event: AstrMessageEvent):
        """修改用户金币"""
        user_id = event.get_sender_id()
        args = event.message_str.split(" ")
        if len(args) < 3:
            yield event.plain_result("❌ 请指定要修改的用户 ID 和金币数量，例如：/修改金币 123456789 1000")
            return
        target_user_id = args[1]
        if not target_user_id.isdigit():
            yield event.plain_result("❌ 用户 ID 必须是数字，请检查后重试。")
            return
        coins = args[2]
        if not coins.isdigit():
            yield event.plain_result("❌ 金币数量必须是数字，请检查后重试。")
            return
        result = self.user_service.modify_user_coins(target_user_id, int(coins))
        if result:
            yield event.plain_result(f"✅ 成功修改用户 {target_user_id} 的金币数量为 {coins} 金币")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("奖励金币")
    async def reward_coins(self, event: AstrMessageEvent):
        """奖励用户金币"""
        args = event.message_str.split(" ")
        if len(args) < 3:
            yield event.plain_result("❌ 请指定要奖励的用户 ID 和金币数量，例如：/奖励金币 123456789 1000")
            return
        target_user_id = args[1]
        if not target_user_id.isdigit():
            yield event.plain_result("❌ 用户 ID 必须是数字，请检查后重试。")
            return
        coins = args[2]
        if not coins.isdigit():
            yield event.plain_result("❌ 金币数量必须是数字，请检查后重试。")
            return
        current_coins = self.user_service.get_user_currency(target_user_id)
        if current_coins is None:
            yield event.plain_result("❌ 用户不存在或未注册，请检查后重试。")
            return
        result = self.user_service.modify_user_coins(target_user_id, int(current_coins.get('coins') + int(coins)))
        if result:
            yield event.plain_result(f"✅ 成功给用户 {target_user_id} 奖励 {coins} 金币")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("扣除金币")
    async def deduct_coins(self, event: AstrMessageEvent):
        """扣除用户金币"""
        args = event.message_str.split(" ")
        if len(args) < 3:
            yield event.plain_result("❌ 请指定要扣除的用户 ID 和金币数量，例如：/扣除金币 123456789 1000")
            return
        target_user_id = args[1]
        if not target_user_id.isdigit():
            yield event.plain_result("❌ 用户 ID 必须是数字，请检查后重试。")
            return
        coins = args[2]
        if not coins.isdigit():
            yield event.plain_result("❌ 金币数量必须是数字，请检查后重试。")
            return
        current_coins = self.user_service.get_user_currency(target_user_id)
        if current_coins is None:
            yield event.plain_result("❌ 用户不存在或未注册，请检查后重试。")
            return
        if int(coins) > current_coins.get('coins'):
            yield event.plain_result("❌ 扣除的金币数量不能超过用户当前拥有的金币数量")
            return
        result = self.user_service.modify_user_coins(target_user_id, int(current_coins.get('coins') - int(coins)))
        if result:
            yield event.plain_result(f"✅ 成功扣除用户 {target_user_id} 的 {coins} 金币")
        else:
            yield event.plain_result("❌ 出错啦！请稍后再试。")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("开启钓鱼后台管理")
    async def start_admin(self, event: AstrMessageEvent):
        if self.web_admin_task and not self.web_admin_task.done():
            yield event.plain_result("❌ 钓鱼后台管理已经在运行中")
            return
        yield event.plain_result("🔄 正在启动钓鱼插件Web管理后台...")

        if not await _is_port_available(self.port):
            yield event.plain_result(f"❌ 端口 {self.port} 已被占用，请更换端口后重试")
            return

        try:
            services_to_inject = {
                "item_template_service": self.item_template_service,
            }
            app = create_app(
                secret_key=self.secret_key,
                services=services_to_inject
            )
            config = Config()
            config.bind = [f"0.0.0.0:{self.port}"]
            self.web_admin_task = asyncio.create_task(serve(app, config))

            # 等待服务启动并获取公网IP
            for i in range(10):
                if await self._check_port_active():
                    break
                await asyncio.sleep(1)
            else:
                raise Exception("⌛ 启动超时，请检查防火墙设置")

            public_ip = await get_public_ip()
            await asyncio.sleep(1)  # 等待服务启动
            if public_ip is None:
                public_ip = "localhost"

            yield event.plain_result(f"✅ 钓鱼后台已启动！\n🔗请访问 http://{public_ip}:{self.port}/admin\n🔑 密钥请到配置文件中查看")
        except Exception as e:
            logger.error(f"启动后台失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 启动后台失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("关闭钓鱼后台管理")
    async def stop_admin(self, event: AstrMessageEvent):
        """关闭钓鱼后台管理"""
        if not hasattr(self, "web_admin_task") or not self.web_admin_task or self.web_admin_task.done():
            yield event.plain_result("❌ 钓鱼后台管理没有在运行中")
            return

        try:
            # 1. 请求取消任务
            self.web_admin_task.cancel()
            # 2. 等待任务实际被取消
            await self.web_admin_task
        except asyncio.CancelledError:
            # 3. 捕获CancelledError，这是成功关闭的标志
            logger.info("钓鱼插件Web管理后台已成功关闭。")
            yield event.plain_result("✅ 钓鱼后台已关闭。")
        except Exception as e:
            # 4. 捕获其他可能的意外错误
            logger.error(f"关闭钓鱼后台管理时发生意外错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 关闭钓鱼后台管理失败: {e}")

    async def _check_port_active(self):
        """验证端口是否实际已激活"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.port),
                timeout=1
            )
            writer.close()
            return True
        except:  # noqa: E722
            return False

    async def terminate(self):
        """插件被卸载/停用时调用"""
        logger.info("钓鱼插件正在终止...")
        self.fishing_service.stop_auto_fishing_task()
        self.achievement_service.stop_achievement_check_task()
        if self.web_admin_task:
            self.web_admin_task.cancel()
        logger.info("钓鱼插件已成功终止。")


    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("批量添加鱼类")
    async def batch_add_fish(self, event: AstrMessageEvent):
        """批量添加鱼类"""
        message_lines = event.message_str.split('\n')
        if len(message_lines) < 2:
            help_text = """📋 批量添加鱼类格式说明：
    /批量添加鱼类
    名称|描述|稀有度|基础价值|最小重量|最大重量|图标URL
    小鲫鱼|一条非常常见的小鱼。|1|10|100|500|None
    泥鳅|滑溜溜的小家伙。|1|15|50|200|None

    注意：
    - 每行一个鱼类数据
    - 字段之间用"|"分隔
    - 稀有度必须是1-5的整数
    - 重量、价值必须是正整数
    - 图标URL可以填None"""
            yield event.plain_result(help_text)
            return
        
        try:
            added_count = 0
            error_count = 0
            errors = []
            
            for i, line in enumerate(message_lines[1:], 2):  # 从第2行开始
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    parts = line.split('|')
                    if len(parts) != 7:
                        errors.append(f"第{i}行: 字段数量不正确，需要7个字段")
                        error_count += 1
                        continue
                    
                    name, description, rarity, base_value, min_weight, max_weight, icon_url = parts
                    
                    # 数据验证
                    if not name.strip():
                        errors.append(f"第{i}行: 鱼类名称不能为空")
                        error_count += 1
                        continue
                    
                    rarity = int(rarity.strip())
                    if not (1 <= rarity <= 5):
                        errors.append(f"第{i}行: 稀有度必须是1-5之间的整数")
                        error_count += 1
                        continue
                    
                    base_value = int(base_value.strip())
                    min_weight = int(min_weight.strip())
                    max_weight = int(max_weight.strip())
                    
                    if base_value < 0:
                        errors.append(f"第{i}行: 基础价值不能为负数")
                        error_count += 1
                        continue
                        
                    if min_weight >= max_weight:
                        errors.append(f"第{i}行: 最小重量必须小于最大重量")
                        error_count += 1
                        continue
                    
                    icon_url = icon_url.strip() if icon_url.strip() != 'None' else None
                    
                    # 添加到数据库
                    fish_data = {
                        "name": name.strip(),
                        "description": description.strip(),
                        "rarity": rarity,
                        "base_value": base_value,
                        "min_weight": min_weight,
                        "max_weight": max_weight,
                        "icon_url": icon_url
                    }
                    
                    self.item_template_service.add_fish_template(fish_data)
                    added_count += 1
                    
                except ValueError as e:
                    errors.append(f"第{i}行: 数据格式错误 - {str(e)}")
                    error_count += 1
                except Exception as e:
                    errors.append(f"第{i}行: 添加失败 - {str(e)}")
                    error_count += 1
            
            # 构建结果消息
            result_msg = f"✅ 批量添加完成！\n📈 成功添加: {added_count} 个鱼类"
            if error_count > 0:
                result_msg += f"\n❌ 失败: {error_count} 个"
                if len(errors) <= 5:  # 只显示前5个错误
                    result_msg += "\n错误详情:\n" + "\n".join(errors)
                else:
                    result_msg += f"\n错误详情(显示前5个):\n" + "\n".join(errors[:5])
                    result_msg += f"\n...还有{len(errors)-5}个错误"
            
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"批量添加鱼类出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 批量添加过程中出现错误: {str(e)}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("批量添加鱼饵")
    async def batch_add_baits(self, event: AstrMessageEvent):
        """批量添加鱼饵"""
        message_lines = event.message_str.split('\n')
        if len(message_lines) < 2:
            help_text = """📋 批量添加鱼饵格式说明：
    /批量添加鱼饵
    名称|描述|稀有度|效果描述|持续时间(分钟)|成本|所需鱼竿稀有度|成功率加成|稀有鱼几率加成|垃圾减少率|价值加成|数量加成|是否消耗品
    普通蚯蚓|最基础的鱼饵，随处可见。|1|无特殊效果|0|5|0|0.0|0.0|0.0|1.0|1.0|True
    红虫|营养丰富的鱼饵，很多鱼都爱吃。|2|提高中小型鱼上钩率|0|20|0|0.05|0.0|0.0|1.0|1.0|True

    注意：
    - 每行一个鱼饵数据
    - 字段之间用"|"分隔  
    - 稀有度必须是1-5的整数
    - 加成值为小数(如0.05表示5%加成)
    - 是否消耗品填True或False"""
            yield event.plain_result(help_text)
            return
        
        try:
            added_count = 0
            error_count = 0
            errors = []
            
            for i, line in enumerate(message_lines[1:], 2):
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    parts = line.split('|')
                    if len(parts) != 13:
                        errors.append(f"第{i}行: 字段数量不正确，需要13个字段")
                        error_count += 1
                        continue
                    
                    (name, description, rarity, effect_description, duration_minutes, 
                    cost, required_rod_rarity, success_rate_modifier, rare_chance_modifier,
                    garbage_reduction_modifier, value_modifier, quantity_modifier, is_consumable) = parts
                    
                    # 数据验证和转换
                    if not name.strip():
                        errors.append(f"第{i}行: 鱼饵名称不能为空")
                        error_count += 1
                        continue
                    
                    rarity = int(rarity.strip())
                    if not (1 <= rarity <= 5):
                        errors.append(f"第{i}行: 稀有度必须是1-5之间的整数")
                        error_count += 1
                        continue
                    
                    bait_data = {
                        "name": name.strip(),
                        "description": description.strip(),
                        "rarity": rarity,
                        "effect_description": effect_description.strip(),
                        "duration_minutes": int(duration_minutes.strip()),
                        "cost": int(cost.strip()),
                        "required_rod_rarity": int(required_rod_rarity.strip()),
                        "success_rate_modifier": float(success_rate_modifier.strip()),
                        "rare_chance_modifier": float(rare_chance_modifier.strip()),
                        "garbage_reduction_modifier": float(garbage_reduction_modifier.strip()),
                        "value_modifier": float(value_modifier.strip()),
                        "quantity_modifier": float(quantity_modifier.strip()),
                        "is_consumable": is_consumable.strip().lower() == 'true'
                    }
                    
                    self.item_template_service.add_bait_template(bait_data)
                    added_count += 1
                    
                except ValueError as e:
                    errors.append(f"第{i}行: 数据格式错误 - {str(e)}")
                    error_count += 1
                except Exception as e:
                    errors.append(f"第{i}行: 添加失败 - {str(e)}")
                    error_count += 1
            
            # 构建结果消息
            result_msg = f"✅ 批量添加完成！\n📈 成功添加: {added_count} 个鱼饵"
            if error_count > 0:
                result_msg += f"\n❌ 失败: {error_count} 个"
                if len(errors) <= 5:
                    result_msg += "\n错误详情:\n" + "\n".join(errors)
                else:
                    result_msg += f"\n错误详情(显示前5个):\n" + "\n".join(errors[:5])
                    result_msg += f"\n...还有{len(errors)-5}个错误"
            
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"批量添加鱼饵出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 批量添加过程中出现错误: {str(e)}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("批量添加鱼竿")
    async def batch_add_rods(self, event: AstrMessageEvent):
        """批量添加鱼竿"""
        message_lines = event.message_str.split('\n')
        if len(message_lines) < 2:
            help_text = """📋 批量添加鱼竿格式说明：
    /批量添加鱼竿
    名称|描述|稀有度|来源|购买价格|质量加成|数量加成|稀有鱼几率加成|耐久度|图标URL
    新手木竿|刚入门时的可靠伙伴|1|shop|50|1.0|1.0|0.0|None|None
    竹制鱼竿|轻巧耐用|2|shop|500|1.0|1.0|0.01|None|None

    注意：
    - 每行一个鱼竿数据
    - 字段之间用"|"分隔
    - 稀有度必须是1-5的整数  
    - 来源必须是shop、gacha或event
    - 加成值为小数(如1.05表示5%加成)
    - 购买价格、耐久度、图标URL可以填None"""
            yield event.plain_result(help_text)
            return
        
        try:
            added_count = 0
            error_count = 0
            errors = []
            
            for i, line in enumerate(message_lines[1:], 2):
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    parts = line.split('|')
                    if len(parts) != 10:
                        errors.append(f"第{i}行: 字段数量不正确，需要10个字段")
                        error_count += 1
                        continue
                    
                    (name, description, rarity, source, purchase_cost, quality_mod, 
                    quantity_mod, rare_mod, durability, icon_url) = parts
                    
                    # 数据验证和转换
                    if not name.strip():
                        errors.append(f"第{i}行: 鱼竿名称不能为空")
                        error_count += 1
                        continue
                    
                    rarity = int(rarity.strip())
                    if not (1 <= rarity <= 5):
                        errors.append(f"第{i}行: 稀有度必须是1-5之间的整数")
                        error_count += 1
                        continue
                    
                    source = source.strip()
                    if source not in ['shop', 'gacha', 'event']:
                        errors.append(f"第{i}行: 来源必须是shop、gacha或event")
                        error_count += 1
                        continue
                    
                    rod_data = {
                        "name": name.strip(),
                        "description": description.strip(),
                        "rarity": rarity,
                        "source": source,
                        "purchase_cost": int(purchase_cost.strip()) if purchase_cost.strip() != 'None' else None,
                        "bonus_fish_quality_modifier": float(quality_mod.strip()),
                        "bonus_fish_quantity_modifier": float(quantity_mod.strip()),
                        "bonus_rare_fish_chance": float(rare_mod.strip()),
                        "durability": int(durability.strip()) if durability.strip() != 'None' else None,
                        "icon_url": icon_url.strip() if icon_url.strip() != 'None' else None
                    }
                    
                    self.item_template_service.add_rod_template(rod_data)
                    added_count += 1
                    
                except ValueError as e:
                    errors.append(f"第{i}行: 数据格式错误 - {str(e)}")
                    error_count += 1
                except Exception as e:
                    errors.append(f"第{i}行: 添加失败 - {str(e)}")
                    error_count += 1
            
            # 构建结果消息
            result_msg = f"✅ 批量添加完成！\n📈 成功添加: {added_count} 个鱼竿"
            if error_count > 0:
                result_msg += f"\n❌ 失败: {error_count} 个"
                if len(errors) <= 5:
                    result_msg += "\n错误详情:\n" + "\n".join(errors)
                else:
                    result_msg += f"\n错误详情(显示前5个):\n" + "\n".join(errors[:5])
                    result_msg += f"\n...还有{len(errors)-5}个错误"
            
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"批量添加鱼竿出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 批量添加过程中出现错误: {str(e)}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("批量添加饰品")
    async def batch_add_accessories(self, event: AstrMessageEvent):
        """批量添加饰品"""
        message_lines = event.message_str.split('\n')
        if len(message_lines) < 2:
            help_text = """📋 批量添加饰品格式说明：
    /批量添加饰品
    名称|描述|稀有度|槽位类型|质量加成|数量加成|稀有鱼几率加成|金币加成|其他加成描述|图标URL
    幸运四叶草|带来好运的小饰品|2|general|1.05|1.0|0.01|1.02|None|None
    渔夫的戒指|刻有古老符文的戒指|3|general|1.0|1.0|0.0|1.10|None|None

    注意：
    - 每行一个饰品数据
    - 字段之间用"|"分隔
    - 稀有度必须是1-5的整数
    - 槽位类型一般填general
    - 加成值为小数(如1.05表示5%加成)
    - 其他加成描述、图标URL可以填None"""
            yield event.plain_result(help_text)
            return
        
        try:
            added_count = 0
            error_count = 0
            errors = []
            
            for i, line in enumerate(message_lines[1:], 2):
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    parts = line.split('|')
                    if len(parts) != 10:
                        errors.append(f"第{i}行: 字段数量不正确，需要10个字段")
                        error_count += 1
                        continue
                    
                    (name, description, rarity, slot_type, quality_mod, quantity_mod, 
                    rare_mod, coin_mod, other_desc, icon_url) = parts
                    
                    # 数据验证和转换
                    if not name.strip():
                        errors.append(f"第{i}行: 饰品名称不能为空")
                        error_count += 1
                        continue
                    
                    rarity = int(rarity.strip())
                    if not (1 <= rarity <= 5):
                        errors.append(f"第{i}行: 稀有度必须是1-5之间的整数")
                        error_count += 1
                        continue
                    
                    accessory_data = {
                        "name": name.strip(),
                        "description": description.strip(),
                        "rarity": rarity,
                        "slot_type": slot_type.strip(),
                        "bonus_fish_quality_modifier": float(quality_mod.strip()),
                        "bonus_fish_quantity_modifier": float(quantity_mod.strip()),
                        "bonus_rare_fish_chance": float(rare_mod.strip()),
                        "bonus_coin_modifier": float(coin_mod.strip()),
                        "other_bonus_description": other_desc.strip() if other_desc.strip() != 'None' else None,
                        "icon_url": icon_url.strip() if icon_url.strip() != 'None' else None
                    }
                    
                    self.item_template_service.add_accessory_template(accessory_data)
                    added_count += 1
                    
                except ValueError as e:
                    errors.append(f"第{i}行: 数据格式错误 - {str(e)}")
                    error_count += 1
                except Exception as e:
                    errors.append(f"第{i}行: 添加失败 - {str(e)}")
                    error_count += 1
            
            # 构建结果消息
            result_msg = f"✅ 批量添加完成！\n📈 成功添加: {added_count} 个饰品"
            if error_count > 0:
                result_msg += f"\n❌ 失败: {error_count} 个"
                if len(errors) <= 5:
                    result_msg += "\n错误详情:\n" + "\n".join(errors)
                else:
                    result_msg += f"\n错误详情(显示前5个):\n" + "\n".join(errors[:5])
                    result_msg += f"\n...还有{len(errors)-5}个错误"
            
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"批量添加饰品出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 批量添加过程中出现错误: {str(e)}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("批量添加帮助")
    async def batch_add_help(self, event: AstrMessageEvent):
        """显示批量添加的帮助信息"""
        help_text = """📋 批量添加功能帮助

    🐟 **批量添加鱼类**
    /批量添加鱼类
    名称|描述|稀有度|基础价值|最小重量|最大重量|图标URL

    🎣 **批量添加鱼竿** 
    /批量添加鱼竿
    名称|描述|稀有度|来源|购买价格|质量加成|数量加成|稀有鱼几率加成|耐久度|图标URL

    🐛 **批量添加鱼饵**
    /批量添加鱼饵
    名称|描述|稀有度|效果描述|持续时间|成本|所需鱼竿稀有度|成功率加成|稀有鱼几率加成|垃圾减少率|价值加成|数量加成|是否消耗品

    💍 **批量添加饰品**
    /批量添加饰品  
    名称|描述|稀有度|槽位类型|质量加成|数量加成|稀有鱼几率加成|金币加成|其他加成描述|图标URL

    **注意事项：**
    - 每行一个物品数据
    - 字段之间用"|"分隔
    - 稀有度必须是1-5的整数
    - None值填写None
    - 小数值如1.05表示5%加成
    - 错误数据会被跳过并显示错误信息"""
        
        yield event.plain_result(help_text)