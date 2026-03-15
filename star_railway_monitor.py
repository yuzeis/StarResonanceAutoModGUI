"""
主程序入口
"""

import json
import logging
import time
import threading
import argparse
import os
import sys
import multiprocessing as mp
from typing import Dict, List, Optional, Any
from logging_config import setup_logging, get_logger
from module_parser import ModuleParser
from module_types import ModuleInfo, normalize_attribute_list, normalize_attribute_name, normalize_category, to_english_attr, CATEGORY_CN_TO_EN
from packet_capture import PacketCapture
from network_interface_util import get_network_interfaces, select_network_interface
from BlueProtobuf_pb2 import CharSerialize

# 多进程保护
_is_main_process = mp.current_process().name == 'MainProcess'

# 获取日志器
logger = get_logger(__name__) if _is_main_process else None


def _tr(lang: str, zh: str, en: str) -> str:
    return en if (lang or '').lower() == 'en' else zh


def get_exec_base_dir() -> str:
    """获取可执行文件所在目录或源码所在目录"""
    try:
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
    except Exception:
        pass
    return os.path.dirname(os.path.abspath(__file__))


class StarResonanceMonitor:
    """星痕共鸣监控器"""
    
    def __init__(self, interface_index: int = None, category: str = "全部", attributes: List[str] = None, 
                 exclude_attributes: List[str] = None, match_count: int = 1, enumeration_mode: bool = False,
                 min_attr_sum: dict | None = None, lang: str = 'zh',
                 combo_size: int = 4, compute_mode: str = 'cpu',
                 generate_vdata: bool = False):
        """
        初始化监控器
        
        Args:
            interface_index: 网络接口索引
            category: 模组类型（攻击/守护/辅助/全部）
            attributes: 要筛选的属性词条列表
            exclude_attributes: 要排除的属性词条列表
            match_count: 模组需要包含的指定词条数量
            enumeration_mode: 是否启用枚举模式
            min_attr_sum: 强制某属性在4件套总和≥VALUE的字典
            generate_vdata: 在线模式下是否将抓到的数据包保存为 modules.vdata
        """
        self.interface_index = interface_index
        self.category = category
        self.attributes = attributes or []
        self.exclude_attributes = exclude_attributes or []
        self.match_count = match_count
        self.min_attr_sum = min_attr_sum or {}
        self.enumeration_mode = enumeration_mode
        self.lang = (lang or 'zh').lower()
        self.combo_size = max(1, min(10, int(combo_size)))
        self.compute_mode = (compute_mode or 'cpu').lower()
        # generate_vdata 仅在在线模式（非 --load-vdata）下有意义
        self.generate_vdata = bool(generate_vdata)
        self.is_running = False
        
        # 获取网络接口信息
        self.interfaces = get_network_interfaces()
        if interface_index is not None and 0 <= interface_index < len(self.interfaces):
            self.selected_interface = self.interfaces[interface_index]
        else:
            self.selected_interface = None
            
        # 初始化组件
        interface_name = self.selected_interface['name'] if self.selected_interface else None
        self.packet_capture = PacketCapture(interface_name)
        self.module_parser = ModuleParser(lang=self.lang)
        
        # 统计数据
        self.stats = {
            'total_packets': 0,
            'sync_container_packets': 0,
            'parsed_modules': 0,
            'players_found': 0,
            'start_time': None
        }
        
        # 存储解析结果
        self.player_modules = {}  # 玩家UID -> 模组列表
        self.module_history = []  # 模组历史记录
        
    def start_monitoring(self):
        """开始监控"""
        self.is_running = True
        self.stats['start_time'] = time.time()
        
        logger.info(_tr(self.lang, "=== 星痕共鸣监控器启动 ===", "=== Star Resonance Monitor Started ==="))
        cat_disp = self.category if self.lang != 'en' else CATEGORY_CN_TO_EN.get(self.category, self.category)
        logger.info(_tr(self.lang, f"模组类型: {self.category}", f"Module Category: {cat_disp}"))
        if self.attributes:
            attrs_disp = self.attributes if self.lang != 'en' else [to_english_attr(a) for a in self.attributes]
            logger.info(_tr(self.lang, f"属性筛选: {', '.join(self.attributes)} (需要包含{self.match_count}个)", f"Attributes: {', '.join(attrs_disp)} (require {self.match_count})"))
        else:
            logger.info(_tr(self.lang, "属性筛选: 无 (使用所有模组)", "Attributes: None (use all modules)"))
        if self.exclude_attributes:
            ex_disp = self.exclude_attributes if self.lang != 'en' else [to_english_attr(a) for a in self.exclude_attributes]
            logger.info(_tr(self.lang, f"排除属性: {', '.join(self.exclude_attributes)}", f"Exclude Attributes: {', '.join(ex_disp)}"))
        if self.selected_interface:
            logger.info(_tr(self.lang, f"网络接口: {self.interface_index} - {self.selected_interface['description']}", f"Network Interface: {self.interface_index} - {self.selected_interface['description']}"))
            logger.info(_tr(self.lang, f"接口名称: {self.selected_interface['name']}", f"Interface Name: {self.selected_interface['name']}"))
            addresses = [addr['addr'] for addr in self.selected_interface['addresses']]
            logger.info(_tr(self.lang, f"接口地址: {', '.join(addresses)}", f"Interface Addresses: {', '.join(addresses)}"))
        else:
            logger.info(_tr(self.lang, "网络接口: 自动", "Network Interface: Auto"))
        
        # 启动抓包
        self.packet_capture.start_capture(self._on_sync_container_data)
        
        
        logger.info(_tr(self.lang, "监控已启动，等待模组数据包, 请重新登录选择角色... (解析完成后将自动退出)", "Monitoring started, waiting for module packets. Please relogin and select character... (Will exit after parsing)"))
        
    def stop_monitoring(self):
        """停止监控"""
        self.is_running = False
        self.packet_capture.stop_capture()
        
        logger.info(_tr(self.lang, "=== 监控已停止 ===", "=== Monitoring Stopped ==="))
        
    def _on_sync_container_data(self, data: Dict[str, Any]):
        """处理SyncContainerData数据包

        注意: 此方法运行在 PacketCapture 的守护线程中，不能调用 sys.exit()。
        优化完成后通过设置 self.is_running = False 通知主线程退出。

        修复: 无论成功还是异常，finally 块都将 is_running 置 False，
        防止服务进程永久阻塞在 while monitor.is_running 循环中。
        """
        self.stats['sync_container_packets'] += 1

        # 防重入：若已完成或已停止，忽略后续重复回调
        if not self.is_running:
            return

        try:
            v_data = data.get('v_data')
            if not v_data:
                # VData 为空（非模组包），继续等待下一个包，不退出
                logger.debug(_tr(self.lang,
                    "收到非模组数据包，继续等待...",
                    "Received non-module packet, waiting..."))
                return

            # ── 可选：保存 vdata 文件 ─────────────────────────────
            if self.generate_vdata:
                try:
                    base_dir = get_exec_base_dir()
                    vdata_path = os.path.join(base_dir, 'modules.vdata')
                    with open(vdata_path, 'wb') as f:
                        f.write(v_data.SerializeToString())
                    logger.info(_tr(self.lang,
                        f"已保存模组数据到: {vdata_path}",
                        f"Saved module data to: {vdata_path}"))
                except Exception as e:
                    logger.warning(_tr(self.lang,
                        f"保存模组数据失败（不影响优化）: {e}",
                        f"Failed to save module data (optimization continues): {e}"))
            else:
                logger.debug(_tr(self.lang,
                    "「生成 vdata」未启用，跳过保存",
                    "generate_vdata disabled, skipping save"))

            # ── 核心：解析 + 优化 ─────────────────────────────────
            self.module_parser.parse_module_info(
                v_data=v_data,
                category=self.category,
                attributes=self.attributes,
                exclude_attributes=self.exclude_attributes,
                match_count=self.match_count,
                enumeration_mode=self.enumeration_mode,
                min_attr_sum=self.min_attr_sum,
                combo_size=self.combo_size,
                compute_mode=self.compute_mode
            )

            # parse_module_info 正常返回 → 优化完成，刷新 stdout
            import sys as _sys
            _sys.stdout.flush()
            logger.info(_tr(self.lang,
                "=== 优化完成，通知主线程退出 ===",
                "=== Optimization done, signaling main thread to exit ==="))

        except Exception as e:
            # 修复: 原来只 log 不退出，导致 is_running 永远为 True，进程挂死
            logger.error(_tr(self.lang,
                f"处理SyncContainerData数据包失败: {e}",
                f"Failed to process SyncContainerData packet: {e}"),
                exc_info=True)
        finally:
            # 无论成功/失败/异常，保证 is_running 被置 False，主线程可以退出
            self.is_running = False
            try:
                self.packet_capture.stop_capture()
            except Exception:
                pass
            

            

def main():
    """主函数"""
    
    parser = argparse.ArgumentParser(description='星痕共鸣模组筛选器')
    parser.add_argument('--interface', '-i', type=int, help='网络接口索引')
    parser.add_argument('--debug', '-d', action='store_true', help='启用调试模式')
    parser.add_argument('--auto', '-a', action='store_true', help='自动检测默认网络接口')
    parser.add_argument('--list', '-l', action='store_true', help='列出所有网络接口')
    parser.add_argument('--category', '-c', type=str, default='全部', help='模组类型/Category (攻击/守护/辅助/全部 或 attack/guardian/support/all)')
    parser.add_argument('--attributes', '-attr', type=str, nargs='+', 
                       help='指定要筛选的属性词条 (例如: 力量加持 敏捷加持 智力加持 特攻伤害 精英打击 特攻治疗加持 专精治疗加持 施法专注 攻速专注 暴击专注 幸运专注 抵御魔法 抵御物理)')
    parser.add_argument('--exclude-attributes', '-exattr', type=str, nargs='+',
                       help='指定要排除的属性词条 (例如: 特攻治疗加持 专精治疗加持)')
    parser.add_argument('--match-count', '-mc', type=int, default=1,
                       help='模组需要包含的指定词条数量 (默认: 1)')
    parser.add_argument('--min-attr-sum', '-mas', nargs=2, action='append', metavar=('ATTR','VALUE'),
                       help='强制某属性在4件套总和≥VALUE。可多次使用，如：-mas 暴击专注 8 -mas 智力加持 12')
    parser.add_argument('--enumeration-mode', '-enum', action='store_true',
                       help='启用枚举模式, 直接使用枚举运算')
    parser.add_argument('--combo-size', '-cs', type=int, default=4,
                       help='组合件数（1~10，默认4）')
    parser.add_argument('--compute-mode', '-cm', type=str, default='cpu',
                       choices=['cpu', 'cuda', 'opencl'],
                       help='计算模式：cpu / cuda / opencl（默认cpu）')
    parser.add_argument('--lang', '-lang', type=str, default='zh', help='输出语言: zh 或 en (默认: zh)')
    parser.add_argument('--load-vdata', '-lv', action='store_true',
                       help='从可执行文件目录读取 modules.vdata, 跳过抓包直接运算')
    parser.add_argument('--generate-vdata', '-gv', action='store_true',
                       help='在线模式下将抓到的 v_data 保存为 modules.vdata（离线模式下自动忽略）')

    args = parser.parse_args()
    # 语言归一
    lang = 'en' if (args.lang or 'zh').lower() == 'en' else 'zh'

    # 归一化输入
    category_cn = normalize_category(args.category)
    attributes_cn = normalize_attribute_list(args.attributes)
    exclude_attributes_cn = normalize_attribute_list(args.exclude_attributes)

    min_attr_sum = {}
    if args.min_attr_sum:
        for name, val in args.min_attr_sum:
            try:
                min_attr_sum[normalize_attribute_name(name)] = int(val)
            except Exception:
                if lang == 'en':
                    print(f"[WARN] Invalid -mas threshold: {name} {val} (must be integer)")
                else:
                    print(f"[WARN] 无效的 -mas 阈值：{name} {val}（应为整数）")
    
    # 设置日志系统
    setup_logging(debug_mode=args.debug)

    # --load-vdata 分支
    if args.load_vdata:
        base_dir = get_exec_base_dir()
        vdata_path = os.path.join(base_dir, 'modules.vdata')
        if not os.path.exists(vdata_path):
            logger.error(_tr(lang, f"找不到离线数据文件: {vdata_path}", f"Offline data file not found: {vdata_path}"))
            sys.exit(1)
        try:
            with open(vdata_path, 'rb') as f:
                data_bytes = f.read()
            char_serialize = CharSerialize()
            char_serialize.ParseFromString(data_bytes)
        except Exception as e:
            logger.error(_tr(lang, f"读取或解析离线数据失败: {e}", f"Failed to read or parse offline data: {e}"))
            sys.exit(1)

        try:
            ModuleParser(lang=lang).parse_module_info(
                v_data=char_serialize,
                category=category_cn,
                attributes=attributes_cn,
                exclude_attributes=exclude_attributes_cn,
                match_count=args.match_count,
                enumeration_mode=args.enumeration_mode,
                min_attr_sum=min_attr_sum,
                combo_size=args.combo_size,
                compute_mode=args.compute_mode
            )
        except Exception as e:
            logger.error(_tr(lang, f"离线计算失败: {e}", f"Offline computation failed: {e}"))
            sys.exit(1)

        sys.exit(0)
        
    # 获取网络接口列表
    interfaces = get_network_interfaces()
    
    if not interfaces:
        logger.error(_tr(lang, "未找到可用的网络接口!", "No available network interfaces found!"))
        return
        
    # 列出网络接口
    if args.list:
        print(_tr(lang, "=== 可用的网络接口 ===", "=== Available Network Interfaces ==="))
        for i, interface in enumerate(interfaces):
            name = interface['name']
            description = interface.get('description', name)
            is_up = "✓" if interface.get('is_up', False) else "✗"
            addresses = [addr['addr'] for addr in interface['addresses']]
            addr_str = ", ".join(addresses) if addresses else _tr(lang, "无IP地址", "No IP addresses")
            
            print(f"  {i:2d}. {is_up} {description}")
            print(_tr(lang, f"      地址: {addr_str}", f"      Addresses: {addr_str}"))
            print(_tr(lang, f"      名称: {name}", f"      Name: {name}"))
            print()
        return
        
    # 确定要使用的接口
    interface_index = None

    if args.auto:
        # 自动检测默认接口（GUI 模式下 stdin 不可用，必须捕获所有异常）
        try:
            from network_interface_util import find_default_network_interface
            interface_index = find_default_network_interface(interfaces)
        except Exception as e:
            logger.debug(f"auto-detect via find_default_network_interface failed: {e}")
            interface_index = None

        if interface_index is None:
            # fallback: 取第一个活动接口，避免 select_network_interface 进入 input() 交互
            for i, iface in enumerate(interfaces):
                if iface.get('is_up', False):
                    interface_index = i
                    logger.info(_tr(lang,
                        f"自动检测未找到默认路由，使用第一个活动接口: {i} - {iface.get('description', iface['name'])}",
                        f"Auto-detect fallback to first active interface: {i} - {iface.get('description', iface['name'])}"))
                    break

        if interface_index is None:
            logger.error(_tr(lang, "未找到可用的网络接口!", "No available network interface found!"))
            sys.exit(1)
        else:
            iface_desc = interfaces[interface_index].get('description', interfaces[interface_index]['name'])
            print(_tr(lang,
                f"使用网络接口: {interface_index} - {iface_desc}",
                f"Using network interface: {interface_index} - {iface_desc}"))

    elif args.interface is not None:
        # 使用指定的接口索引
        if 0 <= args.interface < len(interfaces):
            interface_index = args.interface
        else:
            logger.error(_tr(lang,
                f"无效的接口索引: {args.interface}（共 {len(interfaces)} 个接口）",
                f"Invalid interface index: {args.interface} (total {len(interfaces)} interfaces)"))
            sys.exit(1)
    else:
        # 交互式选择（仅用于命令行直接运行，GUI 永远不走这里）
        print(_tr(lang, "星痕共鸣模组筛选器!", "Star Resonance Module Filter!"))
        print(_tr(lang, "版本: V1.6.5", "Version: V1.6.5"))
        print("GitHub: https://github.com/fudiyangjin/StarResonanceAutoMod")
        print()
        try:
            interface_index = select_network_interface(interfaces)
        except (EOFError, KeyboardInterrupt):
            logger.error(_tr(lang, "未选择网络接口!", "No network interface selected!"))
            sys.exit(1)
        if interface_index is None:
            logger.error(_tr(lang, "未选择网络接口!", "No network interface selected!"))
            sys.exit(1)

    # 创建监控器
    # generate_vdata 仅在在线模式有意义；若用户误在 --load-vdata 时传入，强制忽略
    effective_generate_vdata = getattr(args, 'generate_vdata', False) and not args.load_vdata
    monitor = StarResonanceMonitor(
        interface_index=interface_index,
        category=category_cn,
        attributes=attributes_cn,
        exclude_attributes=exclude_attributes_cn,
        match_count=args.match_count,
        enumeration_mode=args.enumeration_mode,
        min_attr_sum=min_attr_sum,
        lang=lang,
        combo_size=args.combo_size,
        compute_mode=args.compute_mode,
        generate_vdata=effective_generate_vdata
    )
    
    try:
        # 启动监控
        monitor.start_monitoring()

        # 等待模组解析完成
        logger.info(_tr(lang,
            "等待模组数据包... (解析完成后将自动退出)",
            "Waiting for module packets... (Will exit after parsing)"))

        # 增加超时保护（60分钟），防止进程永久阻塞
        _MAX_WAIT = 3600
        _waited = 0
        while monitor.is_running and _waited < _MAX_WAIT:
            time.sleep(0.1)
            _waited += 0.1

        if _waited >= _MAX_WAIT:
            logger.warning(_tr(lang,
                "等待超时（60分钟），强制退出",
                "Wait timeout (60min), force exit"))

    except KeyboardInterrupt:
        logger.info(_tr(lang, "收到停止信号", "Received stop signal"))
    except Exception as e:
        logger.error(_tr(lang, f"在线监控异常: {e}", f"Online monitoring error: {e}"), exc_info=True)
    finally:
        if monitor.is_running:
            monitor.stop_monitoring()
        # 确保所有输出刷新到管道（GUI 从 stdout 读取结果）
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    # 多进程打包支持
    mp.freeze_support()
    main() 