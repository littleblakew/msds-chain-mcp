<!-- 本仓唯一的 agent 说明。CLAUDE.md 只写一行 @AGENTS.md；要加规则改这里，别改 CLAUDE.md。 -->
# msds-chain-mcp

## 这是什么

MSDS Chain 的 MCP server 公开核心（Python；`server.py` 是入口，工具实现按工具组拆在 `msds_mcp/` 下，导入 `server` 即注册全部工具），同时承载各客户端的分发清单（Claude / Codex 插件、Gemini 扩展、npm shim）。
Remote：`github.com/littleblakew/msds-chain-mcp`，**公开仓**，MIT。
分支模型：只有 `main`。push `main` 触发 `deploy.yml`（先跑全量测试再发布容器）；`v*` tag 触发 `release.yml`（发布 npm 与 MCP registry，不可撤销）。
用法与工具清单见 [README.md](README.md)，不在这里重复。

## 怎么跑

```bash
pip install -r requirements-dev.txt      # 运行时依赖 + pytest + 测试脚本要的 httpx2 / PyYAML
MSDS_API_KEY=sk-msds-... python server.py          # stdio 模式（server.py 末尾 mcp.run()）
python server_remote.py                            # HTTP 模式，MSDS_MCP_HOST / MSDS_MCP_PORT（默认 0.0.0.0:8080）
scripts/hooks/install.sh                 # 每个 clone 装一次 git hooks；--check 只报告
```

- 版本号只有一个人工入口：仓根 `VERSION`。改完跑 `scripts/release.sh`（把版本与派生数字写进所有清单并跑守卫），再提交、推 `main`，最后 `git tag vX.Y.Z && git push origin vX.Y.Z`；tag 必须打在改 `VERSION` 的那个 commit 上。
- 代码布局：`msds_mcp/app.py`（配置、`mcp` 实例、`__version__`）→ `params.py` / `render.py` / `calls.py` / `direct.py` 等共享层 → `*_tools.py`（`@mcp.tool` 所在）。`tools/list` 的顺序等于 `server.py` 里各工具模块的 import 顺序，别调换。
- 🔴 `server.<名字>` 的 `setattr`（`monkeypatch` / `patch`）经 `server.py` 里的 `_ServerModule` 传播到所有持有该名字的 `msds_mcp` 模块；由 `global` 重绑的状态（`_call_log_post_failures`）不复制、只从属主模块读。新增这类状态要登记进 `server._LIVE`，`tests/contract/test_server_module_patching.py` 会查。
- 🔴 单文件上限 1500 行（`tests/repo_guards/test_file_length_ratchet.py`，基线 `tests/file_length_baseline.txt`）。读源码的守卫要扫 `server.py` 加 `msds_mcp/` 整组：用 `tests/conftest.py` 的 `server_source()`。
- 注册面快照 `tests/contract/registry_snapshot.json`（`python3 scripts/dump_registry_snapshot.py` 重新生成）：重构不许改它。
- 工具面基线 `published_tool_surface.json` 用 `python3 scripts/export_tool_surface.py` 重新生成。

## 怎么验证

```bash
python -m pytest tests/ -v               # 与 deploy.yml 的 test 步骤一致；必须全绿才能推 main
python -m pytest tests/contract/test_version.py -q   # 改了 VERSION / 清单后的快速检查（release.yml 也跑它）
```

- 工具面守卫 `tests/contract/test_tool_surface_drift.py` 红了不是坏事，是要你判断这次改动对已接入的外部客户端是否安全，确认后再更新基线。
- 新增 scheduled workflow 时，必须把它的 `name:` 登记进 `cron-failure-alert.yml` 的 `workflows:` 清单，`tests/repo_guards/test_cron_alert_coverage.py` 会查。

## 红线

- 🔴 **这是公开仓，推上去就进了别人的 clone，撤回 commit 删不掉历史。** 不许提交：真实语料 / 客户配方（`tests/repo_guards/test_no_real_corpus_in_fixtures.py` 守夹具）、密钥与 token、内部系统名与人名。
- 🔴 **注释和 docstring 不写「某天在哪被撞到、量多小」的事故叙事或运行数字**（`tests/repo_guards/test_no_incident_narrative.py` 按「日期 + 叙事词」查注释与 docstring）。要写就写「为什么现在是这样」。
- 🔴 **`push main` 即发布，没有 promote 闸。** commit message 的**正文**里不许出现 CI 跳过令牌的字面量（GitHub 只认字面量，提到它也会让 CI 静默不跑）；要跳过就把令牌放在 subject。`scripts/hooks/commit-msg` 守这条，没装 hooks 就没人守。
- 🔴 **`release.yml` 必须跑在 GitHub-hosted runner**（`ubuntu-latest`）：npm 的 provenance 拒绝 self-hosted。npm 发布走 OIDC trusted publishing，**别给它加 `NODE_AUTH_TOKEN`**，token 存在时 npm 会优先用它并失败。
- 🔴 `scripts/refresh_backend_surface.py` 在**写盘前**裁掉后端内部面，只留本仓真的调用的 `/api/v2` 端点。**别绕过它手工复制后端导出**，公开仓的历史删不掉。
- 工具的返回文本和描述是对外契约：改措辞、参数名、必填项，先看 `tests/contract/test_description_matches_payload.py` 与工具面守卫，别只改 `server.py`。
- 单副本约束：SSE 传输有状态，`deploy.yml` 把实例固定为 `min=max=1` 并开 sticky sessions。改成多副本之前先想清楚 `/messages/` 会不会 404。
