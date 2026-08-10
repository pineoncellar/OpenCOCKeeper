# -*- coding: utf-8 -*-
"""
@File     :   main.py
@Desc     :   进程入口 — 委托适配器层运行时编排（preflight + 后台固化 Worker + CLI REPL）
@Note     :   实际逻辑见 src/adapter/runtime.py；窗口/世界映射在适配器层完成
"""

from src.adapter.runtime import main


if __name__ == "__main__":
    main()
