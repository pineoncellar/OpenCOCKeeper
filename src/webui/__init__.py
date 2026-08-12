#- encoding: utf-8 -#
#
# @File     :   webui/__init__.py
# @Desc     :   WebUI 控制面：TraceBus 事件总线 + SSE 端点 + 后端 API
# @Note     :   与 src/adapter/web/（数据面）分离，控制面仅提供调试/管理接口，
#              不参与游戏核心管线；Phase 1 仅含 Trace 相关模块
#