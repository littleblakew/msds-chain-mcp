# tests/

按**被测对象**分目录。加测试前先找同主题的已有文件，能追加就追加，确实需要独立夹具时再开新文件。
文件名写它测的是什么，不写票号（`repo_guards/test_test_layout.py` 会查）。

| 目录 | 放什么 |
|---|---|
| `rendering/` | 工具返回给模型的**文本面**：各类披露、警告、附录裁剪、结论措辞 |
| `tools/` | 单个工具的行为与入参：`lang` / `suppliers` / `intent`、报告、计费、prompts |
| `contract/` | 对外契约：工具描述与 schema、工具面快照、structuredContent 透传、后端 API 契约、版本与清单 |
| `transport/` | HTTP 传输层：路由、双传输、请求体上限、鉴权失败、超时、错误码 |
| `call_logging/` | 发给后端的调用日志：记了什么、不许记成什么 |
| `repo_guards/` | 仓库本身的守卫：公开仓的夹具与注释、commit hook、CI 配置、测试布局 |

- 需要进 lifespan 的测试用 `conftest.py` 里 session 级的 `live_client`，别自己 `TestClient(server_remote.app)`（理由在 conftest）。
- 守卫要算仓库根时写 `Path(__file__).resolve().parents[2]`（文件在 `tests/<目录>/` 下）。
