import random
from typing import Dict, Any, List, Tuple
from collections import defaultdict

from astrbot.core.utils.pip_installer import logger
# 导入仓储接口和领域模型
from ..repositories.abstract_repository import (
    AbstractGachaRepository,
    AbstractUserRepository,
    AbstractInventoryRepository,
    AbstractItemTemplateRepository,
    AbstractLogRepository,
    AbstractAchievementRepository
)
from ..domain.models import GachaPool, GachaPoolItem, GachaRecord
from ..utils import get_now


def _perform_single_weighted_draw(pool: GachaPool) -> GachaPoolItem:
    """执行一次加权随机抽奖。"""
    total_weight = sum(item.weight for item in pool.items)
    rand_val = random.uniform(0, total_weight)

    current_weight = 0
    for item in pool.items:
        current_weight += item.weight
        if rand_val <= current_weight:
            return item
    return None # 理论上不会发生


class GachaService:
    """封装与抽卡系统相关的业务逻辑"""

    def __init__(
        self,
        gacha_repo: AbstractGachaRepository,
        user_repo: AbstractUserRepository,
        inventory_repo: AbstractInventoryRepository,
        item_template_repo: AbstractItemTemplateRepository,
        log_repo: AbstractLogRepository,
        achievement_repo: AbstractAchievementRepository
    ):
        self.gacha_repo = gacha_repo
        self.user_repo = user_repo
        self.inventory_repo = inventory_repo
        self.item_template_repo = item_template_repo
        self.achievement_repo = achievement_repo
        self.log_repo = log_repo

    def get_all_pools(self) -> Dict[str, Any]:
        """提供查看所有卡池信息的功能。"""
        try:
            pools = self.gacha_repo.get_all_pools()
            logger.info(f"获取到 {len(pools)} 个卡池信息")
            return {"success": True, "pools": pools}
        except Exception as e:
            return {"success": False, "message": f"获取卡池信息失败: {str(e)}"}

    def get_pool_details(self, pool_id: int) -> Dict[str, Any]:
        """获取单个卡池的详细信息，包括奖品列表和概率。"""
        pool = self.gacha_repo.get_pool_by_id(pool_id)
        if not pool:
            return {"success": False, "message": "该卡池不存在"}

        total_weight = sum(item.weight for item in pool.items)
        if total_weight == 0:
            return {"success": True, "pool": pool, "probabilities": {}}

        probabilities = []
        for item in pool.items:
            probability = float(item.weight / total_weight)
            item_name = "未知物品"
            item_rarity = 1
            if item.item_type == "rod":
                rod = self.item_template_repo.get_rod_by_id(item.item_id)
                item_name = rod.name if rod else "未知鱼竿"
                item_rarity = rod.rarity if rod else 1
            elif item.item_type == "accessory":
                accessory = self.item_template_repo.get_accessory_by_id(item.item_id)
                item_name = accessory.name if accessory else "未知饰品"
                item_rarity = accessory.rarity if accessory else 1
            elif item.item_type == "bait":
                bait = self.item_template_repo.get_bait_by_id(item.item_id)
                item_name = bait.name if bait else "未知鱼饵"
                item_rarity = bait.rarity if bait else 1
            elif item.item_type == "coins":
                item_name = f"{item.quantity} 金币"
            elif item.item_type == "titles":
                item_name = self.item_template_repo.get_title_by_id(item.item_id).name

            probabilities.append({
                "item_type": item.item_type,
                "item_id": item.item_id,
                "item_name": item_name,
                "item_rarity": item_rarity if item.item_type != "titles" else 0,
                "weight": item.weight,
                "probability": probability
            })
        return {"success": True, "pool": pool, "probabilities": probabilities}

    def perform_draw(self, user_id: str, pool_id: int, num_draws: int = 1) -> Dict[str, Any]:
        """
        实现单抽和多连抽的核心逻辑，使用内存聚合 + 批量写入优化。

        Args:
            user_id: 抽卡的用户ID
            pool_id: 卡池ID
            num_draws: 抽卡次数

        Returns:
            一个包含成功状态和抽卡结果的字典。
        """
        user = self.user_repo.get_by_id(user_id)
        if not user:
            return {"success": False, "message": "用户不存在"}

        pool = self.gacha_repo.get_pool_by_id(pool_id)
        if not pool or not pool.items:
            return {"success": False, "message": "卡池不存在或卡池为空"}

        total_cost = pool.cost_coins * num_draws
        if not user.can_afford(total_cost):
            return {"success": False, "message": f"金币不足，需要 {total_cost} 金币"}

        # 1. 执行抽卡 - 在内存中收集所有结果
        draw_results = []
        for _ in range(num_draws):
            drawn_item = _perform_single_weighted_draw(pool)
            if drawn_item:
                draw_results.append(drawn_item)

        if not draw_results:
            return {"success": False, "message": "抽卡失败，请检查卡池配置"}

        # 2. 扣除费用
        user.coins -= total_cost
        self.user_repo.update(user)

        # 3. 内存聚合 + 批量发放奖励
        granted_rewards = self._grant_rewards_batch(user_id, draw_results)
        
        return {"success": True, "results": granted_rewards}

    def _grant_rewards_batch(self, user_id: str, draw_results: List[GachaPoolItem]) -> List[Dict[str, Any]]:
        """
        批量发放奖励，使用内存聚合 + 数据库批量插入优化性能。
        
        Args:
            user_id: 用户ID
            draw_results: 抽奖结果列表
            
        Returns:
            用户可见的奖励列表
        """
        # 内存聚合数据结构
        aggregated_rewards = {
            "rods": [],           # 鱼竿列表 - 现在支持批量插入
            "accessories": [],    # 饰品列表 - 现在支持批量插入  
            "baits": defaultdict(int),  # 鱼饵聚合 {bait_id: total_quantity}
            "coins": 0,          # 金币总数
            "titles": set(),     # 称号集合（避免重复）
        }
        
        # 日志记录数据
        log_records = []
        granted_rewards = []
        
        # 1. 内存聚合阶段
        for item in draw_results:
            if item.item_type == "rod":
                aggregated_rewards["rods"].append(item)
            elif item.item_type == "accessory":
                aggregated_rewards["accessories"].append(item)
            elif item.item_type == "bait":
                aggregated_rewards["baits"][item.item_id] += item.quantity
            elif item.item_type == "coins":
                aggregated_rewards["coins"] += item.quantity
            elif item.item_type == "titles":
                aggregated_rewards["titles"].add(item.item_id)

        # 2. 批量数据库操作阶段
        try:
            db_operations_count = 0
            
            # 批量添加鱼竿 - 使用数据库层批量插入
            if aggregated_rewards["rods"]:
                # 准备批量插入数据
                rod_data_list = []
                rod_templates = {}  # 缓存模板数据
                
                for rod_item in aggregated_rewards["rods"]:
                    rod_template = self.item_template_repo.get_rod_by_id(rod_item.item_id)
                    if rod_template:
                        rod_templates[rod_item.item_id] = rod_template
                        rod_data_list.append((rod_item.item_id, rod_template.durability))
                
                # 执行批量插入
                if rod_data_list:
                    inserted_ids = self.inventory_repo.batch_add_rod_instances(user_id, rod_data_list)
                    db_operations_count += 1  # 一次批量操作
                    
                    # 构建返回结果和日志
                    for i, rod_item in enumerate(aggregated_rewards["rods"]):
                        if rod_item.item_id in rod_templates:
                            rod_template = rod_templates[rod_item.item_id]
                            granted_rewards.append({
                                "type": "rod",
                                "id": rod_item.item_id,
                                "name": rod_template.name,
                                "rarity": rod_template.rarity
                            })
                            # 记录日志
                            self._create_log_record(log_records, user_id, rod_item, rod_template.name, rod_template.rarity)

            # 批量添加饰品 - 使用数据库层批量插入
            if aggregated_rewards["accessories"]:
                # 准备批量插入数据
                accessory_ids = []
                accessory_templates = {}  # 缓存模板数据
                
                for accessory_item in aggregated_rewards["accessories"]:
                    accessory_template = self.item_template_repo.get_accessory_by_id(accessory_item.item_id)
                    if accessory_template:
                        accessory_templates[accessory_item.item_id] = accessory_template
                        accessory_ids.append(accessory_item.item_id)
                
                # 执行批量插入
                if accessory_ids:
                    inserted_ids = self.inventory_repo.batch_add_accessory_instances(user_id, accessory_ids)
                    db_operations_count += 1  # 一次批量操作
                    
                    # 构建返回结果和日志
                    for accessory_item in aggregated_rewards["accessories"]:
                        if accessory_item.item_id in accessory_templates:
                            accessory_template = accessory_templates[accessory_item.item_id]
                            granted_rewards.append({
                                "type": "accessory",
                                "id": accessory_item.item_id,
                                "name": accessory_template.name,
                                "rarity": accessory_template.rarity
                            })
                            # 记录日志
                            self._create_log_record(log_records, user_id, accessory_item, accessory_template.name, accessory_template.rarity)

            # 批量更新鱼饵数量
            if aggregated_rewards["baits"]:
                # 检查是否有batch_update_bait_quantities方法，否则回退到单个更新
                if hasattr(self.inventory_repo, 'batch_update_bait_quantities'):
                    bait_updates = [(bait_id, quantity) for bait_id, quantity in aggregated_rewards["baits"].items()]
                    self.inventory_repo.batch_update_bait_quantities(user_id, bait_updates)
                    db_operations_count += 1  # 一次批量操作
                else:
                    # 回退到原有逻辑
                    for bait_id, total_quantity in aggregated_rewards["baits"].items():
                        if total_quantity > 0:
                            self.inventory_repo.update_bait_quantity(user_id, bait_id, total_quantity)
                            db_operations_count += 1
                
                # 构建返回结果和日志
                for bait_id, total_quantity in aggregated_rewards["baits"].items():
                    bait_template = self.item_template_repo.get_bait_by_id(bait_id)
                    if bait_template and total_quantity > 0:
                        granted_rewards.append({
                            "type": "bait",
                            "id": bait_id,
                            "name": bait_template.name,
                            "rarity": bait_template.rarity,
                            "quantity": total_quantity
                        })
                        # 创建虚拟item用于日志记录
                        virtual_item = type('obj', (object,), {
                            'gacha_pool_id': 0, 
                            'item_type': 'bait', 
                            'item_id': bait_id, 
                            'quantity': total_quantity
                        })
                        self._create_log_record(log_records, user_id, virtual_item, bait_template.name, bait_template.rarity)

            # 批量更新用户金币
            if aggregated_rewards["coins"] > 0:
                user = self.user_repo.get_by_id(user_id)
                user.coins += aggregated_rewards["coins"]
                self.user_repo.update(user)
                db_operations_count += 1
                
                granted_rewards.append({
                    "type": "coins",
                    "quantity": aggregated_rewards["coins"]
                })
                # 创建虚拟item用于日志记录
                virtual_item = type('obj', (object,), {
                    'gacha_pool_id': 0,
                    'item_type': 'coins',
                    'item_id': 0,
                    'quantity': aggregated_rewards["coins"]
                })
                self._create_log_record(log_records, user_id, virtual_item, f"{aggregated_rewards['coins']} 金币", 1)

            # 批量授予称号
            for title_id in aggregated_rewards["titles"]:
                title_template = self.item_template_repo.get_title_by_id(title_id)
                if title_template:
                    self.achievement_repo.grant_title_to_user(user_id, title_id)
                    db_operations_count += 1
                    
                    granted_rewards.append({
                        "type": "title",
                        "id": title_id,
                        "name": title_template.name
                    })
                    # 创建虚拟item用于日志记录
                    virtual_item = type('obj', (object,), {
                        'gacha_pool_id': 0,
                        'item_type': 'titles',
                        'item_id': title_id,
                        'quantity': 1
                    })
                    self._create_log_record(log_records, user_id, virtual_item, title_template.name, 0)

            # 3. 批量写入日志
            if log_records:
                for log_record in log_records:
                    self.log_repo.add_gacha_record(log_record)
                db_operations_count += len(log_records)

            logger.info(f"用户 {user_id} 完成 {len(draw_results)} 次抽奖")
            logger.info(f"批量插入优化: 实际执行 {db_operations_count} 次数据库操作")
            traditional_count = self._estimate_traditional_db_operations(aggregated_rewards)
            logger.info(f"性能提升: 传统方式需要 {traditional_count} 次操作，优化后节省了 {traditional_count - db_operations_count} 次操作")
            if traditional_count > 0:
                improvement = (traditional_count - db_operations_count) / traditional_count * 100
                logger.info(f"性能提升幅度: {improvement:.1f}%")
            
        except Exception as e:
            logger.error(f"批量发放奖励失败: {e}", exc_info=True)
            raise e
            
        return granted_rewards

    def _create_log_record(self, log_records: List[GachaRecord], user_id: str, item, item_name: str, item_rarity: int):
        """创建日志记录"""
        log_entry = GachaRecord(
            record_id=0,  # DB自增
            user_id=user_id,
            gacha_pool_id=item.gacha_pool_id,
            item_type=item.item_type,
            item_id=item.item_id,
            item_name=item_name,
            quantity=item.quantity,
            rarity=item_rarity,
            timestamp=get_now()
        )
        log_records.append(log_entry)

    def _estimate_traditional_db_operations(self, aggregated_rewards: Dict) -> int:
        """估算传统方式需要的数据库操作次数（用于性能对比）"""
        count = 0
        count += len(aggregated_rewards["rods"])  # 鱼竿逐个插入
        count += len(aggregated_rewards["accessories"])  # 饰品逐个插入
        count += len(aggregated_rewards["baits"])  # 鱼饵逐个更新
        count += 1 if aggregated_rewards["coins"] > 0 else 0  # 用户金币更新
        count += len(aggregated_rewards["titles"])  # 称号逐个授予
        return count

    def _grant_reward(self, user_id: str, item: GachaPoolItem):
        """
        传统的单个奖励发放方法，保留用于兼容性。
        注意：此方法已被_grant_rewards_batch替代，不推荐在新代码中使用。
        """
        item_name = "未知物品"
        item_rarity = 1
        template = None

        if item.item_type == "rod":
            self.inventory_repo.add_rod_instance(user_id, item.item_id, None) # 假设新获得的鱼竿耐久度是满的
            template = self.item_template_repo.get_rod_by_id(item.item_id)
        elif item.item_type == "accessory":
            self.inventory_repo.add_accessory_instance(user_id, item.item_id)
            template = self.item_template_repo.get_accessory_by_id(item.item_id)
        elif item.item_type == "bait":
            self.inventory_repo.update_bait_quantity(user_id, item.item_id, item.quantity)
            template = self.item_template_repo.get_bait_by_id(item.item_id)
        elif item.item_type == "coins":
            user = self.user_repo.get_by_id(user_id)
            user.coins += item.quantity
            self.user_repo.update(user)
            item_name = f"{item.quantity} 金币"
        elif item.item_type == "titles":
            # 注意：成就仓储负责授予称号
            self.achievement_repo.grant_title_to_user(user_id, item.item_id)
            template = self.item_template_repo.get_title_by_id(item.item_id)

        if template:
            item_name = template.name
            item_rarity = template.rarity if hasattr(template, "rarity") else 1

        # 记录日志
        log_entry = GachaRecord(
            record_id=0, # DB自增
            user_id=user_id,
            gacha_pool_id=item.gacha_pool_id,
            item_type=item.item_type,
            item_id=item.item_id,
            item_name=item_name,
            quantity=item.quantity,
            rarity=item_rarity,
            timestamp=get_now()
        )
        self.log_repo.add_gacha_record(log_entry)

    def get_user_gacha_history(self, user_id: str, limit: int = 10) -> Dict[str, Any]:
        """提供查询抽卡历史记录的功能。"""
        records = self.log_repo.get_gacha_records(user_id, limit)
        return {"success": True, "records": records}