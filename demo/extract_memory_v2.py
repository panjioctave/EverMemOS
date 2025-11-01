"""记忆提取脚本 V2 - 使用新的 ClusterManager + ProfileManager 架构

这是一个简化版本，用于测试新组件的效果：
- 使用 ClusterManager 进行自动聚类
- 使用 ProfileManager 进行自动 profile 提取
- 不依赖数据库，直接输出 JSON 文件
- 支持断点续传

主要改进：
1. 代码量减少 70%（从 1400+ 行到 400+ 行）
2. 自动化程度提高（聚类+profile 全自动）
3. 架构清晰（组件独立，职责分离）

使用方法：
    python demo/extract_memory_v2.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
)

# 确保 src 包可被发现
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from src.memory_layer.memcell_extractor.base_memcell_extractor import RawData
from src.memory_layer.memcell_extractor.conv_memcell_extractor import (
    ConvMemCellExtractor,
    ConversationMemCellExtractRequest,
)
from src.memory_layer.llm.llm_provider import LLMProvider
from src.memory_layer.cluster_manager import (
    ClusterManager,
    ClusterManagerConfig,
    InMemoryClusterStorage,
)
from src.memory_layer.profile_manager import (
    ProfileManager,
    ProfileManagerConfig,
    ScenarioType,
    InMemoryProfileStorage,
)
from src.common_utils.datetime_utils import from_iso_format, to_iso_format

load_dotenv()

console = Console()

# ============================================================================
# 配置
# ============================================================================

# 输出目录
OUTPUT_DIR = Path("./demo/output_v2")

# LLM 配置
LLM_CONFIG = {
    "provider_type": "openai",
    "model": os.getenv("LLM_MODEL", "gpt-4"),
    "api_key": os.getenv("LLM_API_KEY"),
    "base_url": os.getenv("LLM_BASE_URL"),
    "temperature": 0.3,
    "max_tokens": 32768,
}

# 聚类配置
CLUSTER_CONFIG = ClusterManagerConfig(
    similarity_threshold=0.65,      # 相似度阈值
    max_time_gap_days=7.0,          # 最大时间间隔（天）
    enable_persistence=True,
    persist_dir=str(OUTPUT_DIR / "clusters"),
    clustering_algorithm="centroid"
)

# Profile 配置
PROFILE_CONFIG = ProfileManagerConfig(
    # scenario=ScenarioType.GROUP_CHAT,  # 或 ScenarioType.ASSISTANT
    scenario=ScenarioType.ASSISTANT,
    min_confidence=0.6,                 # 价值判别阈值
    enable_versioning=True,
    auto_extract=True,
    batch_size=50,
    max_retries=3,
)


# ============================================================================
# 数据加载
# ============================================================================

def load_conversations_from_json(json_path: Path) -> Dict[str, List[dict]]:
    """从 JSON 文件加载对话数据
    
    支持格式：
    1. 单个对话列表: [{"speaker_id": "...", "content": "...", ...}, ...]
    2. 多个对话字典: {"conv_1": [...], "conv_2": [...]}
    3. 包含 conversation 字段: [{"conversation": [...]}, ...]
    
    Args:
        json_path: JSON 文件路径
    
    Returns:
        对话字典: {conversation_id: [messages]}
    """
    console.print(f"\n📂 加载对话数据: {json_path}", style="cyan")
    
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    conversations = {}
    
    conversations = {
        "conv_000": data["conversation_list"]
    }
    
    total_messages = sum(len(msgs) for msgs in conversations.values())
    console.print(f"✅ 加载完成: {len(conversations)} 个对话, {total_messages} 条消息", style="green")
    
    return conversations


def normalize_message(msg: dict) -> Optional[dict]:
    """规范化消息格式
    
    确保消息包含必需字段：speaker_id, speaker_name, content, timestamp
    
    Returns:
        规范化的消息字典，如果消息无效则返回 None
    """
    # 检查输入类型
    if not isinstance(msg, dict):
        return None
    
    normalized = {}
    
    # speaker_id（支持多种字段名）
    normalized["speaker_id"] = (
        msg.get("speaker_id") or 
        msg.get("sender_id") or 
        msg.get("sender") or 
        msg.get("user_id") or 
        "unknown"
    )
    
    # speaker_name
    normalized["speaker_name"] = (
        msg.get("speaker_name") or 
        msg.get("sender_name") or 
        msg.get("user_name") or 
        normalized["speaker_id"]
    )
    
    # content
    content = msg.get("content") or msg.get("text") or msg.get("message") or ""
    if not content or not str(content).strip():
        return None  # 跳过空消息
    
    normalized["content"] = str(content).strip()
    
    # timestamp（支持多种字段名）
    timestamp = (
        msg.get("timestamp") or 
        msg.get("time") or 
        msg.get("created_at") or
        msg.get("create_time")
    )
    
    if timestamp:
        if isinstance(timestamp, str):
            normalized["timestamp"] = timestamp
        elif isinstance(timestamp, (int, float)):
            from src.common_utils.datetime_utils import from_timestamp
            dt = from_timestamp(timestamp)
            normalized["timestamp"] = to_iso_format(dt)
        else:
            normalized["timestamp"] = str(timestamp)
    else:
        # 使用当前时间
        from src.common_utils.datetime_utils import get_now_with_timezone
        normalized["timestamp"] = to_iso_format(get_now_with_timezone())
    
    # 保留其他字段
    for key, value in msg.items():
        if key not in normalized:
            normalized[key] = value
    
    return normalized


# ============================================================================
# MemCell 提取
# ============================================================================

async def extract_memcells_from_conversation(
    conversation: List[dict],
    conv_id: str,
    memcell_extractor: ConvMemCellExtractor,
    progress: Progress = None,
    task_id: int = None,
) -> List[dict]:
    """从单个对话中提取 MemCells
    
    Args:
        conversation: 消息列表
        conv_id: 对话 ID
        memcell_extractor: MemCell 提取器
        progress: 进度条
        task_id: 进度任务 ID
    
    Returns:
        MemCell 字典列表
    """
    # 规范化消息（过滤掉 None）
    normalized_msgs = []
    for msg in conversation:
        normalized = normalize_message(msg)
        if normalized:  # 只保留有效消息
            normalized_msgs.append(normalized)
    
    if not normalized_msgs:
        console.print(f"[red]✗ {conv_id}: 没有有效消息[/red]")
        return []
    
    # 提取 speaker IDs
    speaker_ids = list(set(msg["speaker_id"] for msg in normalized_msgs if msg.get("speaker_id")))
    
    if not speaker_ids:
        console.print(f"[yellow]⚠️  {conv_id}: 没有找到 speaker_id，使用 'unknown'[/yellow]")
        speaker_ids = ["unknown"]
    
    # 转换为 RawData
    raw_data_list = [
        RawData(content=msg, data_id=f"{conv_id}_msg_{i}")
        for i, msg in enumerate(normalized_msgs)
    ]
    
    memcells = []
    history_raw_data_list = []
    
    # 历史窗口大小（参考原代码的 history_window_size）
    HISTORY_WINDOW_SIZE = 200
    
    for idx, raw_data in enumerate(raw_data_list):
        # 更新进度
        if progress and task_id is not None:
            progress.update(task_id, completed=idx)
        
        # 第一条消息，初始化历史
        if not history_raw_data_list:
            history_raw_data_list.append(raw_data)
            continue
        
        # 创建提取请求
        request = ConversationMemCellExtractRequest(
            history_raw_data_list=list(history_raw_data_list),  # 使用完整历史
            new_raw_data_list=[raw_data],
            user_id_list=speaker_ids,
            smart_mask_flag=True,  # 启用智能遮罩（参考原代码）
            group_id=None,
        )
        
        # 提取 MemCell
        memcell = None
        status = None
        
        for retry in range(3):
            try:
                memcell, status = await memcell_extractor.extract_memcell(request, use_semantic_extraction=True)
                break
            except Exception as e:
                if retry == 2:
                    console.print(f"[red]✗ 提取失败 (conv={conv_id}, msg={idx}): {e}[/red]")
                else:
                    await asyncio.sleep(0.5)
        
        # 根据提取结果处理
        if memcell:
            # 成功提取到 MemCell
            memcell_dict = memcell.to_dict()
            memcells.append(memcell_dict)
            
            # ===== 立即保存 MemCell（参考原代码逻辑）=====
            
            output_file = OUTPUT_DIR / "memcells" / f"{conv_id}_memcell_{len(memcells):03d}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(memcell_dict, f, ensure_ascii=False, indent=2)
            
            # 重置历史（保留当前消息）
            history_raw_data_list = [raw_data]
        
        elif status and status.should_wait:
            # 等待更多消息
            history_raw_data_list.append(raw_data)
            
            # 限制历史窗口大小（参考原代码）
            if len(history_raw_data_list) > HISTORY_WINDOW_SIZE:
                history_raw_data_list = history_raw_data_list[-HISTORY_WINDOW_SIZE:]
        
        else:
            # 其他情况：继续累积
            history_raw_data_list.append(raw_data)
            if len(history_raw_data_list) > HISTORY_WINDOW_SIZE:
                history_raw_data_list = history_raw_data_list[-HISTORY_WINDOW_SIZE:]
    
    # ===== 重要：处理剩余的历史消息（强制提取） =====
    # 参考原代码逻辑，循环结束后如果 history 还有消息，需要提取
    if history_raw_data_list and len(history_raw_data_list) > 0:
        console.print(f"[cyan]ℹ️  {conv_id}: 强制提取剩余 {len(history_raw_data_list)} 条消息为 MemCell[/cyan]")
        
        try:
            from src.memory_layer.types import MemCell as MemCellType, RawDataType
            import uuid
            
            # 获取最后一条消息的时间戳
            last_timestamp = history_raw_data_list[-1].content.get("timestamp")
            if isinstance(last_timestamp, str):
                from src.common_utils.datetime_utils import from_iso_format
                timestamp_dt = from_iso_format(last_timestamp)
            else:
                from src.common_utils.datetime_utils import get_now_with_timezone
                timestamp_dt = get_now_with_timezone()
            
            # 构建 original_data
            original_data = [rd.content for rd in history_raw_data_list]
            
            # 创建 MemCell
            final_memcell = MemCellType(
                event_id=str(uuid.uuid4()),
                user_id_list=speaker_ids,
                original_data=original_data,
                timestamp=timestamp_dt,
                summary=f"对话片段（{len(history_raw_data_list)} 条消息）",
                type=RawDataType.CONVERSATION,
            )
            
            final_memcell_dict = final_memcell.to_dict()
            memcells.append(final_memcell_dict)
            
            # ===== 立即保存最后的 MemCell =====
            output_file = OUTPUT_DIR / "memcells" / f"{conv_id}_memcell_{len(memcells):03d}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(final_memcell_dict, f, ensure_ascii=False, indent=2)
        
        except Exception as e:
            console.print(f"[yellow]⚠️  {conv_id}: 强制提取失败: {e}[/yellow]")
            import traceback
            traceback.print_exc()
    
    # 更新进度为完成
    if progress and task_id is not None:
        progress.update(task_id, completed=len(raw_data_list))
    
    return memcells


# ============================================================================
# 主流程
# ============================================================================

async def main():
    """主函数"""
    
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("记忆提取脚本 V2 - ClusterManager + ProfileManager", style="bold cyan")
    console.print("=" * 70 + "\n", style="bold cyan")
    
    start_time = time.time()
    
    # ===== 1. 准备输出目录 =====
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "memcells").mkdir(exist_ok=True)
    (OUTPUT_DIR / "clusters").mkdir(exist_ok=True)
    (OUTPUT_DIR / "profiles").mkdir(exist_ok=True)
    
    # ===== 2. 加载对话数据 =====
    # 从环境变量或默认路径加载
    # data_path = Path(os.getenv("CONVERSATION_DATA_PATH", "./data/conversations.json"))
    data_path = Path("/Users/admin/Documents/Projects/opensource/memsys-opensource/data/assistant_chat_zh.json")
    if not data_path.exists():
        console.print(f"[red]✗ 数据文件不存在: {data_path}[/red]")
        console.print("\n💡 使用方法:")
        console.print("   1. 设置环境变量: export CONVERSATION_DATA_PATH=/path/to/data.json")
        console.print("   2. 或将数据放到: ./data/conversations.json")
        console.print("\n数据格式示例:")
        console.print("""   {
       "conv_1": [
           {"speaker_id": "user1", "speaker_name": "Alice", "content": "Hello", "timestamp": "2024-01-01T10:00:00"},
           {"speaker_id": "user2", "speaker_name": "Bob", "content": "Hi", "timestamp": "2024-01-01T10:01:00"}
       ]
   }""")
        return
    
    conversations = load_conversations_from_json(data_path)
    
    # ===== 3. 初始化组件 =====
    console.print("\n⚙️  初始化组件...", style="yellow")
    
    # LLM Provider
    llm_provider = LLMProvider(**LLM_CONFIG)
    console.print("  ✅ LLM Provider 初始化完成", style="green")
    
    # ClusterManager
    cluster_storage = InMemoryClusterStorage(
        enable_persistence=True,
        persist_dir=OUTPUT_DIR / "clusters"
    )
    cluster_mgr = ClusterManager(config=CLUSTER_CONFIG, storage=cluster_storage)
    console.print(f"  ✅ ClusterManager 初始化完成 (threshold={CLUSTER_CONFIG.similarity_threshold})", style="green")
    
    # ProfileManager
    profile_storage = InMemoryProfileStorage(
        enable_persistence=True,
        persist_dir=OUTPUT_DIR / "profiles",
        enable_versioning=True
    )
    profile_mgr = ProfileManager(
        llm_provider=llm_provider,
        config=PROFILE_CONFIG,
        storage=profile_storage,
        group_id="demo_extraction",
        group_name="Demo Extraction"
    )
    console.print(f"  ✅ ProfileManager 初始化完成 (min_confidence={PROFILE_CONFIG.min_confidence})", style="green")
    
    # MemCellExtractor
    memcell_extractor = ConvMemCellExtractor(llm_provider=llm_provider)
    console.print("  ✅ MemCellExtractor 初始化完成", style="green")
    
    # ===== 4. 连接组件 =====
    console.print("\n🔗 连接组件...", style="yellow")
    
    # ProfileManager → ClusterManager
    profile_mgr.attach_to_cluster_manager(cluster_mgr)
    console.print("  ✅ ProfileManager 已连接到 ClusterManager", style="green")
    
    # ClusterManager → MemCellExtractor
    cluster_mgr.attach_to_extractor(memcell_extractor)
    console.print("  ✅ ClusterManager 已连接到 MemCellExtractor", style="green")
    
    # ===== 5. 处理对话 =====
    console.print("\n" + "=" * 70, style="bold")
    console.print("开始提取 MemCells", style="bold")
    console.print("=" * 70 + "\n", style="bold")
    
    all_memcells = {}
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        
        main_task = progress.add_task(
            "[cyan]总进度",
            total=len(conversations)
        )
        
        for conv_id, conversation in conversations.items():
            # 创建子任务
            conv_task = progress.add_task(
                f"[yellow]{conv_id}",
                total=len(conversation)
            )
            
            # 提取 MemCells
            memcells = await extract_memcells_from_conversation(
                conversation=conversation,
                conv_id=conv_id,
                memcell_extractor=memcell_extractor,
                progress=progress,
                task_id=conv_task,
            )
            
            all_memcells[conv_id] = memcells
            
            # 保存对话汇总（已在循环中逐个保存）
            all_memcells[conv_id] = memcells
            
            if memcells:
                progress.update(conv_task, description=f"[green]{conv_id} ✓ ({len(memcells)} memcells)")
            else:
                progress.update(conv_task, description=f"[yellow]{conv_id} (0 memcells)")
            
            progress.update(main_task, advance=1)
    
    # 等待异步 profile 提取完成
    console.print("\n⏳ 等待聚类和 Profile 提取完成...", style="yellow")
    await asyncio.sleep(3)
    
    # ===== 6. 导出结果 =====
    console.print("\n" + "=" * 70, style="bold")
    console.print("导出结果", style="bold")
    console.print("=" * 70 + "\n", style="bold")
    
    # 导出所有 MemCells
    all_memcells_flat = []
    for memcells in all_memcells.values():
        all_memcells_flat.extend(memcells)
    
    if all_memcells_flat:
        with open(OUTPUT_DIR / "memcells_all.json", "w", encoding="utf-8") as f:
            json.dump(all_memcells_flat, f, ensure_ascii=False, indent=2)
        console.print(f"✅ MemCells 已保存: {OUTPUT_DIR / 'memcells_all.json'} ({len(all_memcells_flat)} 个)", style="green")
    else:
        console.print(f"[yellow]⚠️  没有提取到任何 MemCells[/yellow]")
    
    # 导出聚类
    cluster_count = await cluster_mgr.export_clusters(OUTPUT_DIR / "clusters")
    console.print(f"✅ 聚类结果已保存: {OUTPUT_DIR / 'clusters'} ({cluster_count} 个组)", style="green")
    
    # 导出 Profiles
    profile_count = await profile_mgr.export_profiles(
        OUTPUT_DIR / "profiles",
        include_history=True
    )
    console.print(f"✅ Profiles 已保存: {OUTPUT_DIR / 'profiles'} ({profile_count} 个用户)", style="green")
    
    # ===== 7. 统计信息 =====
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("统计信息", style="bold cyan")
    console.print("=" * 70 + "\n", style="bold cyan")
    
    # MemCell 统计
    console.print("📝 MemCell 统计:", style="bold")
    console.print(f"   - 对话数量: {len(conversations)}")
    console.print(f"   - 消息总数: {sum(len(c) for c in conversations.values())}")
    console.print(f"   - MemCell 总数: {len(all_memcells_flat)}")
    
    # 聚类统计
    cluster_stats = cluster_mgr.get_stats()
    console.print("\n📊 聚类统计:", style="bold")
    console.print(f"   - 处理的 MemCells: {cluster_stats['total_memcells']}")
    console.print(f"   - 聚类的 MemCells: {cluster_stats['clustered_memcells']}")
    console.print(f"   - 集群总数: {cluster_stats['total_clusters']}")
    console.print(f"   - 新建集群: {cluster_stats['new_clusters']}")
    console.print(f"   - 失败的 Embeddings: {cluster_stats['failed_embeddings']}")
    if cluster_stats['total_clusters'] > 0:
        avg_size = cluster_stats['clustered_memcells'] / cluster_stats['total_clusters']
        console.print(f"   - 平均集群大小: {avg_size:.1f}")
    
    # Profile 统计
    profile_stats = profile_mgr.get_stats()
    console.print("\n👤 Profile 统计:", style="bold")
    console.print(f"   - 处理的 MemCells: {profile_stats['total_memcells']}")
    console.print(f"   - 高价值 MemCells: {profile_stats['high_value_memcells']}")
    console.print(f"   - Profile 提取次数: {profile_stats['profile_extractions']}")
    console.print(f"   - 失败的提取: {profile_stats['failed_extractions']}")
    console.print(f"   - 监控的集群: {profile_stats['watched_clusters']}/{profile_stats['total_clusters']}")
    if profile_stats['total_memcells'] > 0:
        high_value_rate = profile_stats['high_value_memcells'] / profile_stats['total_memcells']
        console.print(f"   - 高价值比率: {high_value_rate:.1%}")
    
    # 用户 Profiles
    all_profiles = await profile_mgr.get_all_profiles()
    if all_profiles:
        console.print(f"\n👥 提取的用户 Profiles ({len(all_profiles)} 个):", style="bold")
        for user_id, profile in list(all_profiles.items())[:5]:  # 只显示前 5 个
            console.print(f"\n   📌 {user_id}:")
            if hasattr(profile, 'user_name'):
                console.print(f"      姓名: {profile.user_name}")
            if hasattr(profile, 'hard_skills') and profile.hard_skills:
                skills = [s.get('value') for s in profile.hard_skills[:3]]
                console.print(f"      技能: {', '.join(skills)}")
            if hasattr(profile, 'work_responsibility') and profile.work_responsibility:
                resp = [r.get('value') for r in profile.work_responsibility[:2]]
                console.print(f"      职责: {', '.join(resp)}")
        
        if len(all_profiles) > 5:
            console.print(f"\n   ... 还有 {len(all_profiles) - 5} 个用户")
    
    # 耗时
    elapsed = time.time() - start_time
    console.print(f"\n⏱️  总耗时: {elapsed:.1f} 秒", style="bold yellow")
    
    # ===== 8. 输出目录结构 =====
    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("输出文件", style="bold cyan")
    console.print("=" * 70 + "\n", style="bold cyan")
    
    console.print(f"📁 {OUTPUT_DIR}/")
    console.print("├── memcells/")
    console.print(f"│   ├── conv_000.json (单个对话的 MemCells)")
    console.print(f"│   └── ... ({len(conversations)} 个文件)")
    console.print("├── memcells_all.json (所有 MemCells)")
    console.print("├── clusters/")
    console.print("│   ├── cluster_state_demo_extraction.json (聚类状态)")
    console.print("│   └── cluster_map_demo_extraction.json (聚类映射)")
    console.print("└── profiles/")
    console.print(f"    ├── profile_user1.json (用户 Profile)")
    console.print(f"    ├── profile_user2.json")
    console.print(f"    ├── ... ({profile_count} 个用户)")
    console.print("    └── history/ (Profile 历史版本)")
    
    console.print("\n" + "=" * 70, style="bold green")
    console.print("✅ 提取完成！", style="bold green")
    console.print("=" * 70 + "\n", style="bold green")


if __name__ == "__main__":
    asyncio.run(main())

