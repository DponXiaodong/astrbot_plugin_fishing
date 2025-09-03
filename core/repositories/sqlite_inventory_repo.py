import sqlite3
import threading
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from astrbot.api import logger

# 导入抽象基类和领域模型
from .abstract_repository import AbstractInventoryRepository
from ..domain.models import UserFishInventoryItem, UserRodInstance, UserAccessoryInstance, FishingZone


class SqliteInventoryRepository(AbstractInventoryRepository):
    """用户库存仓储的SQLite实现"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()

    def _get_connection(self) -> sqlite3.Connection:
        """获取一个线程安全的数据库连接。"""
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.connection = conn
        return conn

    # --- 私有映射辅助方法 ---
    def _row_to_fish_item(self, row: sqlite3.Row) -> Optional[UserFishInventoryItem]:
        if not row:
            return None
        return UserFishInventoryItem(**row)

    def _row_to_rod_instance(self, row: sqlite3.Row) -> Optional[UserRodInstance]:
        if not row:
            return None
        return UserRodInstance(**row)

    def _row_to_accessory_instance(self, row: sqlite3.Row) -> Optional[UserAccessoryInstance]:
        if not row:
            return None
        return UserAccessoryInstance(**row)

    # --- Fish Inventory Methods ---
    def get_fish_inventory(self, user_id: str) -> List[UserFishInventoryItem]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, fish_id, quantity FROM user_fish_inventory WHERE user_id = ? AND quantity > 0", (user_id,))
            return [self._row_to_fish_item(row) for row in cursor.fetchall()]

    def get_fish_inventory_value(self, user_id: str, rarity: Optional[int] = None) -> int:
        query = """
            SELECT SUM(f.base_value * ufi.quantity)
            FROM user_fish_inventory ufi
            JOIN fish f ON ufi.fish_id = f.fish_id
            WHERE ufi.user_id = ?
        """
        params = [user_id]
        if rarity is not None:
            query += " AND f.rarity = ?"
            params.append(rarity)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            result = cursor.fetchone()
            return result[0] if result and result[0] is not None else 0

    def add_fish_to_inventory(self, user_id: str, fish_id: int, quantity: int = 1) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_fish_inventory (user_id, fish_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, fish_id) DO UPDATE SET quantity = quantity + excluded.quantity
            """, (user_id, fish_id, quantity))
            conn.commit()

    def clear_fish_inventory(self, user_id: str, rarity: Optional[int] = None) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if rarity is None:
                cursor.execute("DELETE FROM user_fish_inventory WHERE user_id = ?", (user_id,))
            else:
                cursor.execute("""
                    DELETE FROM user_fish_inventory
                    WHERE user_id = ? AND fish_id IN (
                        SELECT fish_id FROM fish WHERE rarity = ?
                    )
                """, (user_id, rarity))
            conn.commit()

    def sell_fish_keep_one(self, user_id: str) -> int:
        """
        执行“保留一条”的卖出数据库操作。
        返回卖出的总价值。
        注意：此操作应在一个事务中完成，以保证数据一致性。
        """
        sold_value = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            try:
                # 查询所有数量大于1的鱼及其价值
                cursor.execute("""
                    SELECT ufi.fish_id, ufi.quantity, f.base_value, f.name
                    FROM user_fish_inventory ufi
                    JOIN fish f ON ufi.fish_id = f.fish_id
                    WHERE ufi.user_id = ? AND ufi.quantity > 1
                """, (user_id,))

                items_to_sell = cursor.fetchall()

                if not items_to_sell:
                    conn.rollback()
                    return 0

                for item in items_to_sell:
                    sell_qty = item["quantity"] - 1
                    sold_value += sell_qty * item["base_value"]

                # 将所有数量大于1的鱼更新为1
                cursor.execute("""
                    UPDATE user_fish_inventory
                    SET quantity = 1
                    WHERE user_id = ? AND quantity > 1
                """, (user_id,))

                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise # 向上抛出异常，让服务层处理
        return sold_value

    def get_user_equipped_rod(self, user_id: str) -> Optional[UserRodInstance]:
        """获取用户当前装备的钓竿实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM user_rods
                WHERE user_id = ? AND is_equipped = 1
            """, (user_id,))
            row = cursor.fetchone()
            return self._row_to_rod_instance(row) if row else None

    def get_user_rod_instance_by_id(self, user_id: str, rod_instance_id: int) -> Optional[UserRodInstance]:
        """根据用户ID和钓竿实例ID获取特定的钓竿实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM user_rods
                WHERE user_id = ? AND rod_instance_id = ?
            """, (user_id, rod_instance_id))
            row = cursor.fetchone()
            return self._row_to_rod_instance(row) if row else None

    def clear_user_rod_instances(self, user_id: str) -> None:
        """清空用户的所有未装备和低于五星的钓竿实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM user_rods
                WHERE user_id = ? AND is_equipped = 0 AND rod_id NOT IN (
                    SELECT rod_id FROM rods WHERE rarity >= 5
                )
            """, (user_id,))
            conn.commit()

    def clear_user_accessory_instances(self, user_id: str) -> None:
        """清空用户的所有未装备和低于五星的配件实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM user_accessories
                WHERE user_id = ? AND is_equipped = 0 AND accessory_id NOT IN (
                    SELECT accessory_id FROM accessories WHERE rarity >= 5
                )
            """, (user_id,))
            conn.commit()

    def get_user_accessory_instance_by_id(self, user_id: str, accessory_instance_id: int) -> Optional[UserAccessoryInstance]:
        """根据用户ID和配件实例ID获取特定的配件实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM user_accessories
                WHERE user_id = ? AND accessory_instance_id = ?
            """, (user_id, accessory_instance_id))
            row = cursor.fetchone()
            return self._row_to_accessory_instance(row) if row else None

    def get_user_equipped_accessory(self, user_id: str) -> Optional[UserAccessoryInstance]:
        """获取用户当前装备的配件实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM user_accessories
                WHERE user_id = ? AND is_equipped = 1
            """, (user_id,))
            row = cursor.fetchone()
            return self._row_to_accessory_instance(row) if row else None

    def set_equipment_status(self, user_id: str, rod_instance_id: Optional[int] = None, accessory_instance_id: Optional[int] = None) -> None:
        """
        设置用户的装备状态。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 重置所有装备状态
            cursor.execute("""
                UPDATE user_rods SET is_equipped = 0 WHERE user_id = ?
            """, (user_id,))
            cursor.execute("""
                UPDATE user_accessories SET is_equipped = 0 WHERE user_id = ?
            """, (user_id,))

            # 设置新的装备状态
            if rod_instance_id is not None:
                cursor.execute("""
                    UPDATE user_rods SET is_equipped = 1 WHERE rod_instance_id = ? AND user_id = ?
                """, (rod_instance_id, user_id))
            if accessory_instance_id is not None:
                cursor.execute("""
                    UPDATE user_accessories SET is_equipped = 1 WHERE accessory_instance_id = ? AND user_id = ?
                """, (accessory_instance_id, user_id))

            conn.commit()


    def get_user_disposable_baits(self, user_id: str) -> List[int]:
        """
        获取用户的可用诱饵列表。
        返回一个包含诱饵ID的列表。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT bait_id FROM user_bait_inventory
                WHERE user_id = ? AND quantity > 0
            """, (user_id,))
            return [row["bait_id"] for row in cursor.fetchall()]

    def get_user_titles(self, user_id: str) -> List[int]:
        """
        获取用户拥有的称号列表。
        返回一个包含称号ID的列表。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT title_id FROM user_titles
                WHERE user_id = ?
            """, (user_id,))
            return [row["title_id"] for row in cursor.fetchall()]

    def get_random_bait(self, user_id: str) -> Optional[int]:
        """
        从用户的诱饵库存中随机获取一个可用的诱饵ID。
        如果没有可用诱饵，则返回None。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT bait_id FROM user_bait_inventory
                WHERE user_id = ? AND quantity > 0
                ORDER BY RANDOM() LIMIT 1
            """, (user_id,))
            row = cursor.fetchone()
            return row["bait_id"] if row else None

    # --- Bait Inventory Methods ---
    def get_user_bait_inventory(self, user_id: str) -> Dict[int, int]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT bait_id, quantity FROM user_bait_inventory WHERE user_id = ?", (user_id,))
            return {row["bait_id"]: row["quantity"] for row in cursor.fetchall()}

    def update_bait_quantity(self, user_id: str, bait_id: int, delta: int) -> None:
        """更新用户诱饵库存中特定诱饵的数量（可增可减），并确保数量不小于0。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_bait_inventory (user_id, bait_id, quantity)
                VALUES (?, ?, MAX(0, ?))
                ON CONFLICT(user_id, bait_id) DO UPDATE SET quantity = MAX(0, quantity + ?)
            """, (user_id, bait_id, delta, delta))
            # 删除数量为0的行，保持数据整洁
            cursor.execute("DELETE FROM user_bait_inventory WHERE user_id = ? AND quantity <= 0", (user_id,))
            conn.commit()


    # --- Rod Inventory Methods ---
    def get_user_rod_instances(self, user_id: str) -> List[UserRodInstance]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_rods WHERE user_id = ?", (user_id,))
            return [self._row_to_rod_instance(row) for row in cursor.fetchall()]

    def  add_rod_instance(self, user_id: str, rod_id: int, durability: Optional[int], refine_level:int = 1) -> UserRodInstance:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now()
            cursor.execute("""
                INSERT INTO user_rods (user_id, rod_id, current_durability, obtained_at, refine_level, is_equipped)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (user_id, rod_id, durability, now, refine_level))
            instance_id = cursor.lastrowid
            conn.commit()
            return UserRodInstance(
                rod_instance_id=instance_id, user_id=user_id, rod_id=rod_id,
                is_equipped=False, obtained_at=now, current_durability=durability, refine_level=refine_level
            )

    def delete_rod_instance(self, rod_instance_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_rods WHERE rod_instance_id = ?", (rod_instance_id,))
            conn.commit()

    # --- Accessory Inventory Methods ---
    def get_user_accessory_instances(self, user_id: str) -> List[UserAccessoryInstance]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_accessories WHERE user_id = ?", (user_id,))
            return [self._row_to_accessory_instance(row) for row in cursor.fetchall()]

    def add_accessory_instance(self, user_id: str, accessory_id: int, refine_level: int = 1) -> UserAccessoryInstance:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now()
            cursor.execute("""
                INSERT INTO user_accessories (user_id, accessory_id, obtained_at, refine_level, is_equipped)
                VALUES (?, ?, ?, ?, 0)
            """, (user_id, accessory_id, now, refine_level))
            instance_id = cursor.lastrowid
            conn.commit()
            return UserAccessoryInstance(
                accessory_instance_id=instance_id, user_id=user_id, accessory_id=accessory_id,
                is_equipped=False, obtained_at=now, refine_level=refine_level
            )

    def delete_accessory_instance(self, accessory_instance_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_accessories WHERE accessory_instance_id = ?", (accessory_instance_id,))
            conn.commit()

    def update_fish_quantity(self, user_id: str, fish_id: int, delta: int) -> None:
        """更新用户鱼类库存中特定鱼的数量（可增可减），并确保数量不小于0。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_fish_inventory (user_id, fish_id, quantity)
                VALUES (?, ?, MAX(0, ?))
                ON CONFLICT(user_id, fish_id) DO UPDATE SET quantity = MAX(0, quantity + ?)
            """, (user_id, fish_id, delta, delta))
            # 删除数量为0的行，保持数据整洁
            cursor.execute("DELETE FROM user_fish_inventory WHERE user_id = ? AND quantity <= 0", (user_id,))
            conn.commit()
    def get_zone_by_id(self, zone_id: int) -> FishingZone:
        """根据ID获取钓鱼区域信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM fishing_zones WHERE id = ?", (zone_id,))
            row = cursor.fetchone()
            if row:
                return FishingZone(**row)
            else:
                raise ValueError(f"钓鱼区域ID {zone_id} 不存在。")
    def update_fishing_zone(self, zone: FishingZone) -> None:
        """更新钓鱼区域信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE fishing_zones
                SET name = ?, description = ?, daily_rare_fish_quota = ?, rare_fish_caught_today = ?
                WHERE id = ?
            """, (zone.name, zone.description, zone.daily_rare_fish_quota, zone.rare_fish_caught_today, zone.id))
            conn.commit()

    def get_all_fishing_zones(self) -> List[FishingZone]:
        """获取所有钓鱼区域信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM fishing_zones")
            return [FishingZone(**row) for row in cursor.fetchall()]

    def update_rod_instance(self, rod_instance: UserRodInstance):
        """更新钓竿实例信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_rods
                SET rod_id = ?, is_equipped = ?, current_durability = ?, refine_level = ?
                WHERE rod_instance_id = ? AND user_id = ?
            """, (rod_instance.rod_id, rod_instance.is_equipped, rod_instance.current_durability, rod_instance.refine_level, rod_instance.rod_instance_id, rod_instance.user_id))
            conn.commit()

    def update_accessory_instance(self, accessory_instance: UserAccessoryInstance):
        """更新配件实例信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_accessories
                SET accessory_id = ?, is_equipped = ?, refine_level = ?
                WHERE accessory_instance_id = ? AND user_id = ?
            """, (accessory_instance.accessory_id, accessory_instance.is_equipped, accessory_instance.refine_level, accessory_instance.accessory_instance_id, accessory_instance.user_id))
            conn.commit()

    def get_same_rod_instances(self, user_id: int, rod_id: str) -> List[UserRodInstance]:
        """获取用户所有相同类型的钓竿实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM user_rods
                WHERE user_id = ? AND rod_id = ?
            """, (user_id, rod_id))
            return [self._row_to_rod_instance(row) for row in cursor.fetchall()]

    def get_same_accessory_instances(self, user_id: int, accessory_id: str) -> List[UserAccessoryInstance]:
        """获取用户所有相同类型的配件实例"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM user_accessories
                WHERE user_id = ? AND accessory_id = ?
            """, (user_id, accessory_id))
            return [self._row_to_accessory_instance(row) for row in cursor.fetchall()]

    def batch_add_rod_instances(self, user_id: str, rod_data_list: List[Tuple[int, int]]) -> List[int]:
        """
        批量添加鱼竿实例。如果批量插入失败，会自动回退到单个插入。
        
        Args:
            user_id: 用户ID
            rod_data_list: 鱼竿数据列表，每个元素为 (rod_id, durability)
        
        Returns:
            新创建的鱼竿实例ID列表
        """
        if not rod_data_list:
            return []
            
        # 首先尝试批量插入
        try:
            return self._batch_insert_rod_instances(user_id, rod_data_list)
        except Exception as e:
            logger.warning(f"批量插入鱼竿失败，回退到单个插入: {e}")
            # 回退到单个插入
            return self._fallback_add_rod_instances(user_id, rod_data_list)
    
    def _batch_insert_rod_instances(self, user_id: str, rod_data_list: List[Tuple[int, int]]) -> List[int]:
        """实际的批量插入实现"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            try:
                # 开始事务
                conn.execute("BEGIN TRANSACTION")
                
                # 构建批量插入语句 - 使用正确的表名和字段名
                placeholders = ",".join(["(?, ?, ?, CURRENT_TIMESTAMP, 0, 1)"] * len(rod_data_list))  # is_equipped=0, refine_level=1
                sql = f"""
                INSERT INTO user_rods (user_id, rod_id, current_durability, obtained_at, is_equipped, refine_level)
                VALUES {placeholders}
                """
                
                # 准备参数：user_id, rod_id, current_durability 为一组
                params = []
                for rod_id, durability in rod_data_list:
                    params.extend([user_id, rod_id, durability])
                
                cursor.execute(sql, params)
                
                # 获取插入的ID范围
                last_id = cursor.lastrowid
                first_id = last_id - len(rod_data_list) + 1
                inserted_ids = list(range(first_id, last_id + 1))
                
                # 提交事务
                conn.commit()
                
                logger.info(f"批量插入 {len(rod_data_list)} 个鱼竿实例成功，ID范围: {first_id}-{last_id}")
                return inserted_ids
                
            except Exception as e:
                # 回滚事务
                conn.rollback()
                raise e
    
    def _fallback_add_rod_instances(self, user_id: str, rod_data_list: List[Tuple[int, int]]) -> List[int]:
        """回退方案：单个插入鱼竿实例"""
        inserted_ids = []
        for rod_id, durability in rod_data_list:
            try:
                # 使用现有的单个插入方法
                instance_id = self.add_rod_instance(user_id, rod_id, durability)
                if instance_id:
                    inserted_ids.append(instance_id)
            except Exception as e:
                logger.error(f"插入单个鱼竿实例失败 (rod_id: {rod_id}): {e}")
                continue
        
        logger.info(f"回退方案完成：成功插入 {len(inserted_ids)} 个鱼竿实例")
        return inserted_ids
    
    def batch_add_accessory_instances(self, user_id: str, accessory_ids: List[int]) -> List[int]:
        """
        批量添加饰品实例。如果批量插入失败，会自动回退到单个插入。
        
        Args:
            user_id: 用户ID
            accessory_ids: 饰品ID列表
            
        Returns:
            新创建的饰品实例ID列表
        """
        if not accessory_ids:
            return []
            
        # 首先尝试批量插入
        try:
            return self._batch_insert_accessory_instances(user_id, accessory_ids)
        except Exception as e:
            logger.warning(f"批量插入饰品失败，回退到单个插入: {e}")
            # 回退到单个插入
            return self._fallback_add_accessory_instances(user_id, accessory_ids)
    
    def _batch_insert_accessory_instances(self, user_id: str, accessory_ids: List[int]) -> List[int]:
        """实际的批量插入实现"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            try:
                # 开始事务
                conn.execute("BEGIN TRANSACTION")
                
                # 构建批量插入语句 - 使用正确的表名和字段名
                placeholders = ",".join(["(?, ?, CURRENT_TIMESTAMP, 0, 1)"] * len(accessory_ids))  # is_equipped=0, refine_level=1
                sql = f"""
                INSERT INTO user_accessories (user_id, accessory_id, obtained_at, is_equipped, refine_level)
                VALUES {placeholders}
                """
                
                # 准备参数：user_id, accessory_id 为一组
                params = []
                for accessory_id in accessory_ids:
                    params.extend([user_id, accessory_id])
                
                cursor.execute(sql, params)
                
                # 获取插入的ID范围
                last_id = cursor.lastrowid
                first_id = last_id - len(accessory_ids) + 1
                inserted_ids = list(range(first_id, last_id + 1))
                
                # 提交事务
                conn.commit()
                
                logger.info(f"批量插入 {len(accessory_ids)} 个饰品实例成功，ID范围: {first_id}-{last_id}")
                return inserted_ids
                
            except Exception as e:
                # 回滚事务
                conn.rollback()
                raise e
    
    def _fallback_add_accessory_instances(self, user_id: str, accessory_ids: List[int]) -> List[int]:
        """回退方案：单个插入饰品实例"""
        inserted_ids = []
        for accessory_id in accessory_ids:
            try:
                # 使用现有的单个插入方法
                instance_id = self.add_accessory_instance(user_id, accessory_id)
                if instance_id:
                    inserted_ids.append(instance_id)
            except Exception as e:
                logger.error(f"插入单个饰品实例失败 (accessory_id: {accessory_id}): {e}")
                continue
        
        logger.info(f"回退方案完成：成功插入 {len(inserted_ids)} 个饰品实例")
        return inserted_ids
    
    def batch_update_bait_quantities(self, user_id: str, bait_updates: List[Tuple[int, int]]) -> None:
        """
        批量更新鱼饵数量。
        
        Args:
            user_id: 用户ID
            bait_updates: 更新列表，每个元素为 (bait_id, delta_quantity)
        """
        if not bait_updates:
            return
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            try:
                # 开始事务
                conn.execute("BEGIN TRANSACTION")
                
                for bait_id, delta_quantity in bait_updates:
                    if delta_quantity == 0:
                        continue
                        
                    # 检查当前数量 - 使用正确的表名
                    cursor.execute("""
                        SELECT quantity FROM user_bait_inventory 
                        WHERE user_id = ? AND bait_id = ?
                    """, (user_id, bait_id))
                    
                    result = cursor.fetchone()
                    if result:
                        new_quantity = max(0, result[0] + delta_quantity)
                        cursor.execute("""
                            UPDATE user_bait_inventory 
                            SET quantity = ? 
                            WHERE user_id = ? AND bait_id = ?
                        """, (new_quantity, user_id, bait_id))
                    elif delta_quantity > 0:
                        # 新增记录
                        cursor.execute("""
                            INSERT INTO user_bait_inventory (user_id, bait_id, quantity)
                            VALUES (?, ?, ?)
                        """, (user_id, bait_id, delta_quantity))
                
                # 提交事务
                conn.commit()
                
                logger.info(f"批量更新 {len(bait_updates)} 种鱼饵数量")
                
            except Exception as e:
                # 回滚事务
                conn.rollback()
                logger.error(f"批量更新鱼饵数量失败: {e}")
                raise e