# -*- coding: utf-8 -*-
"""
FC2 流出检测器 - 启动脚本

此文件是程序的入口点，负责启动主程序并执行初始化工作
"""

import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# 检查必要的依赖是否已安装
try:
    import bs4
    import requests
    import rich
except ImportError as e:
    missing_lib = str(e).split("'")[1]
    print(f"\n错误: 缺少必要的库 '{missing_lib}'")
    print("\n请使用以下命令安装所需依赖:")
    print("pip install -r requirements.txt")
    print("\n安装完成后再次运行程序。")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s: %(message)s", level=logging.INFO
)

# 导入i18n模块并初始化
try:
    from src.utils.i18n import initialize as init_i18n, get_text
    # 先初始化i18n
    init_i18n()
    # 设置翻译函数
    _ = get_text
except ImportError as e:
    # 如果i18n模块导入失败，使用简单的替代函数
    _ = lambda key, default: default
    print(f"\n警告: 无法加载i18n模块: {e}")
    print("将使用默认语言显示。")

@contextmanager
def time_tracker(description: str):
    """跟踪程序运行时间的上下文管理器"""
    start_time = datetime.now()
    
    # 使用安全的方式获取翻译文本
    startup_time = _('startup_time', '启动于')
        
    print(f"\n=== {description} {startup_time} {start_time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    try:
        yield
    finally:
        end_time = datetime.now()
        duration = end_time - start_time
        
        # 使用安全的方式获取翻译文本
        time_minutes = _('time_minutes', '分')
        time_seconds = _('time_seconds', '秒')
        program_end = _('program_end', '程序运行结束')
        time_spent = _('time_spent', '耗时')
            
        duration_str = f"{duration.seconds // 60}{time_minutes}{duration.seconds % 60}{time_seconds}"
        print(f"\n=== {program_end}，{time_spent}: {duration_str} ===\n")


def main() -> int:
    """程序主入口函数"""
    
    try:
        # 检查Python版本
        if sys.version_info < (3, 8):
            print(_("errors.python_version", "错误: 需要Python 3.8或更高版本"))
            sys.exit(1)

        # 初始化配置（会自动创建必要的目录）
        from config import config

        # 显示启动信息
        with time_tracker(_("app_name", "FC2流出检测器")):
            # 导入并启动主程序
            from main import main as run_main

            exit_code = run_main()

        return exit_code
    except KeyboardInterrupt:
        print(f"\n\n{_('error_interrupted', '程序被用户中断')}")
        return 130  # SIGINT标准退出码
    except Exception as e:
        print(f"\n{_('error_startup', '程序启动错误')}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
