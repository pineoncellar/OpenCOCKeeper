# -*- coding: utf-8 -*-
"""
@File     :   main.py
@Desc     :   进程入口 — 委托适配器层运行时编排（preflight + 后台固化 Worker + WebUI 调试窗口）
@Note     :   实际逻辑见 src/adapter/runtime.py；终端不接收输入，交互走 WebUI，仅输出日志
"""

from src.adapter.runtime import main


if __name__ == "__main__":
    main()
