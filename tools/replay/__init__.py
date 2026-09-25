"""回放台：用真实聊天记录与归档状态，回归验收 narrative 的行为。

设计要点：
- 数据路径一律 CLI 传入，**归档数据不进仓库**（ADR-0005）
- 用例以 TDD 方式先红后绿：``case_*`` 返回失败列表，空列表＝通过
- 插件模块经 ``runtime.load`` 挂到合成包加载，绕开 maibot_sdk

见 ``cases/case_privacy_leak.py`` —— 批 1 的首个用例（日记/涉私泄露回归）。
"""
