#!/usr/bin/env python3
"""重新生成工具面基线 `published_tool_surface.json` —— CI-848。

跑法（在仓根）：`python3 scripts/export_tool_surface.py`

**什么时候跑**：守卫红了、你确认这次改动可以放行之后。跑完 **必须** 手工把
`listing_resubmitted_at` 这类字段更新成事实——脚本不知道你有没有真的去 OpenAI
后台重交，它只会照抄当前注册表。🔴 **别让这个脚本替你宣布「条目已经同步了」。**
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from tool_surface import extract_surface  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "published_tool_surface.json"


def main() -> None:
    tools = asyncio.run(server.mcp.list_tools())
    surface = extract_surface(tools)
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    doc = {
        "_ticket": "CI-848",
        "_why": (
            "外部客户端拿到的是工具面的**快照**：ChatGPT 应用目录条目在 Scan Tools 时拍一份"
            "（官方原话 Published plugins do not update those skills live），claude.ai 在添加连接器时拍一份。"
            "平台都不会告诉我们条目过期了，所以这份基线＝上一次有人**看过并接受**的工具面。"
        ),
        "_how_to_update": "改动被接受后跑 scripts/export_tool_surface.py，并手工核对下面两个日期字段。",
        "last_reviewed": existing.get("last_reviewed", "填今天的日期"),
        "chatgpt_listing_resubmitted_at": existing.get("chatgpt_listing_resubmitted_at"),
        "tools": surface,
    }
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"已写入 {OUT.name}：{len(surface)} 个工具")
    print("🔴 别忘了手工更新 last_reviewed；重交过上架条目才动 chatgpt_listing_resubmitted_at")


if __name__ == "__main__":
    main()
