# git hooks（`.git/hooks/` 不受版本控制 ⇒ 每个 clone 都要装一次）

```bash
scripts/hooks/install.sh          # 装（幂等）
scripts/hooks/install.sh --check  # 只报告，退出码非零＝有没装上的
```

🔴 **别在这份 README 里维护「装哪几个」的名单**（CI-685）：`install.sh` 自己扫本目录，
凡是名字是 git 认识的钩子名的都装。理由是那种名单会腐化，而**漏装的新钩子与
「装了但没触发」完全同形** —— 谁都不会发现。
🔴 worktree 共享主仓的 `.git/hooks`（`$GIT_COMMON_DIR/hooks`）⇒ **装一次覆盖所有 worktree**，
但**必须从主 checkout 装**：链接指向本次 checkout 的路径，worktree 一删就悬空，
**而悬空时 git 静默跳过钩子**。`install.sh` 会拒绝在 linked worktree 里跑。
🔴 已存在但不是本仓链接的钩子**不会被覆盖**，只报告 —— 悄悄换掉别人的钩子比不装更糟。

---

## `commit-msg` — 正文里的 CI 跳过令牌（CI-800，上游 CI-685）

**为什么要机械闸门而不是纪律**：GitHub 只认字面量、不管你是在「用」它还是在
「谈论」它。已经害过四次（2026-08-20 / 08-23 / 08-31 ×2）。第三次是在改 msds-chain 的
`promote-prod.sh` 时犯的，**而那个文件顶部就写着这个坑** —— 读过、写过、仍然犯。

🔴 **本仓比 msds-chain 更危险，这是它需要这道闸的全部理由**：msds-chain 那边 CI 静默
不跑，还有 promote gate 在 `develop→main` 那一刻兜住；**本仓 push `main` 即部署、
没有 promote gate** ⇒ 静默不跑＝**静默不发布**。

第四次就发生在这一族仓里（2026-08-31，`msds-chain-gateway`）：merge commit 正文里
解释「为什么这次不加那个令牌」⇒ 整轮 CI 静默不跑，push 成功、远端有 sha、零报错，
而 Prod 镜像停在上一版。**发现它只能靠「main 变了但镜像 tag 没变」**——
没有任何东西会报错，`git push` 的输出和成功的那次逐字相同。

**判据**：令牌在 **body**、但不在 **subject**。这切在真实用法上——合法跳过把令牌写在
subject，事故形态只出现在 body。
🔴 别放宽成「整条 message 有没有令牌」：那会拦掉每一次合法的跳过提交，
于是大家第一时间学会 `--no-verify`，闸当天就死。

**两条出路**（所以没有环境变量逃生口）：真想跳过 ⇒ 把令牌挪到标题；
只是在谈论 ⇒ 换个写法（`skip-ci` ／「跳过令牌」／把方括号拆开）。兜底 `--no-verify`。

**令牌清单只有一份**：`scripts/lib/ci-skip-token.sh`。任何要判「这段文本里有没有令牌」
的地方都 source 它，**加载不上一律硬失败** —— 函数没定义时返回 127，`if !` 会把它
变成真 ⇒ 静默退化成「从来查不到令牌」，而那与「这段文本确实干净」完全同形。

---

## 自动化用例

`tests/test_commit_msg_hook.py`。🔴 **这里不写条数**——写死的数字下次就过期，
而过期的数字读起来和准确的一模一样。要数就现数：`pytest --collect-only -q`。

🔴 **判据不是「文件在仓里」**。钩子不受版本控制 ⇒ 文件躺在仓里但没人跑 `install.sh`
时，这道闸**完全不存在**，而仓看起来和装好了一模一样。所以端到端那条用例造一个
**全新的仓**、拷进这三个文件、跑一次 `install.sh`、再真的 `git commit` ——
验的是「新克隆装一次之后真的拦得住」。

🔴 **每条守卫的变异配方写在它自己的 docstring 里**，改动钩子后按那些配方各跑一遍
——**没记变异的守卫默认当它不存在**。

---

## 🔴 这套文件是副本，四个仓各一份

`msds-chain` · `msds-chain-gateway` · `msds-chain-mcp` · `msds-chain-mcp-gateway`
各有一份 `scripts/hooks/commit-msg` + `scripts/hooks/install.sh` +
`scripts/lib/ci-skip-token.sh`。四个独立 git repo、各自要能被单独 clone 且自洽
⇒ 没法指向一处真相源。**代价是漂移不报错**：改了一份、另外三份还是旧的，
而旧的那份仍然会跑、仍然是绿的、和「已经更新了」完全同形。

**两级判据，别混为一谈**：

① **本族三个仓之间必须逐字相同**（这条是硬的，可机械判）：

```bash
cd <workspace>/products
for f in scripts/hooks/commit-msg scripts/hooks/install.sh scripts/hooks/README.md \
         scripts/lib/ci-skip-token.sh tests/test_commit_msg_hook.py; do
  for r in msds-chain-gateway msds-chain-mcp msds-chain-mcp-gateway; do
    [ -e "$r/$f" ] || { echo "MISSING: $r/$f  ← 先看这个，别读成 DRIFT"; continue; }
    diff -q "msds-chain-gateway/$f" "$r/$f" >/dev/null || echo "DRIFT: $r/$f"
  done
done
```

🔴 **`MISSING` 和 `DRIFT` 必须分开报**（CI-800 review 抓到的）：三个仓不是同一刻
落地的，任一仓还没合 main 时这条 diff 会**整片报红**，而**基线本来就是红的判据
不构成任何证据** —— 下一个人只会学会忽略它。先确认三个仓都有文件，再谈漂移。

② **与 msds-chain 是同源但有已知差异**，别拿 ①那条 diff 去比它，会全红：
   · `commit-msg` 指向的用例路径不同（那边 `backend/tests/scripts/`，这边 `tests/`）
   · `commit-msg` / README 里「为什么需要它」不同（那边有 promote gate 兜底，这边没有）
   · `install.sh` 的用法示例路径不同
   · `msds-chain` 另有本仓专属的 `pre-push`（判据是 `docs/**.md` 推 `develop`），
     其余三个仓没有 `develop` 也没有那个布局 ⇒ **有意不移植**
   ⇒ 改**逻辑**（判据、令牌清单、路径解析）时四个仓一起改；改**叙事**时按各仓实际情况写。

⚠️ 两条判据**都不是自动的**——没有任何东西会在你只改了一份的时候变红。
这是本方案已知且刻意接受的缺口（理由：四个仓必须能被单独 clone）。
真要机械化，得先给这三个文件找一个 vendored 分发通道（先例＝`lagentbot-chat`）。
