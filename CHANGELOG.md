# Changelog

## v1.0.0 (2026-11-17)

首个正式发布。

### 会话来源(9 个)
- Claude Code、Codex
- OpenCode 2.x(sqlite,全功能编辑:seq 分配器同步 + backup API 快照 + 行级外部修改守卫)
- Gemini CLI、Qwen Code(目录哈希反查还原项目名)
- Goose、Cline、Roo Code
- Kiro IDE(探测式解析 workspaceStorage)

### 核心功能
- 统一列表:浏览/筛选/排序/按项目分组/统计,来源徽章专属配色
- 全局搜索:jsonl 行搜 / JSON 记录搜 / sqlite 全文三路分派,命中跳转到消息
- 编辑:文本 + Raw JSON 双模式;删除自动连带工具调用配对;Claude 自动修复 parentUuid 链
- 转生:任意来源 → Claude / Codex / OpenCode / Gemini / Qwen / Goose / Cline / Roo / JSON
- 意识上传:自包含可移植包,目标机器零依赖恢复
- 批处理:批量收藏/归档/导出/删除/上传/转生
- 中英双语界面

### 工程化
- 持久化扫描缓存(311MB 扫描 1.03s → 0.03s)
- 懒加载 + 后台线程,连点不吞点击
- 写前自动备份 / 外部修改检测 / 原子写入
- GitHub Actions:CI selftest + 打包,tag 触发自动 Release
- MIT License + 免责声明
