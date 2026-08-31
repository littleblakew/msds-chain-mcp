"""`scripts/hooks/commit-msg` —— 别让「谈论跳过令牌」把整轮 CI 静默关掉（CI-800／上游 CI-685）。

失效形态：GitHub 只认字面量，不管你是在**用**它还是在**谈论**它。命中之后
整轮 workflow 不跑，而 `git push` 一切正常、远端有 sha ⇒ **「零个 run」和
「CI 还没起来」完全同形**，人只会以为 GitHub 慢。

🔴 **本仓比 msds-chain 更危险，这是这道闸被移植过来的全部理由**：那边 CI 静默不跑
   还有 promote gate 在 `develop→main` 兜住；**本仓 push `main` 即部署、没有 promote
   gate** ⇒ 静默不跑＝**静默不发布**。2026-08-31 在 `msds-chain-gateway` 真实发生过：
   merge commit 正文里解释「为什么这次不加那个令牌」⇒ 整轮 CI 不跑，push 成功、
   远端有 sha、零报错，而 Prod 镜像停在上一版。**发现它只能靠「main 变了但镜像
   tag 没变」**——没有任何东西会报错。

判据是「令牌在 body、不在 subject」——它切在真实用法上：合法跳过写在 subject，
事故形态只出现在 body。

🔴 **判据不是「文件在仓里」**。钩子不受版本控制 ⇒ 文件躺在仓里但没人跑
   `install.sh` 时这道闸**完全不存在**，而仓看起来和装好了一模一样。
   ⇒ 下面凡是端到端的用例都造一个**全新的仓**、拷进文件、跑一次 `install.sh`、
   再真的 `git commit`，验的是「新克隆装一次之后真的拦得住」。

🔴 **本文件绝不写裸的令牌字面量**，一律用 `TOKEN` 变量拼。理由不是洁癖：
   真正的风险是有人复制这里的一行到 commit message 里；更要紧的是，按字面量
   grep 源码的守卫会命中散文里的样本，于是「自检报空跑」而空跑的是样本不是规则。
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "scripts" / "hooks" / "commit-msg"
INSTALL = REPO_ROOT / "scripts" / "hooks" / "install.sh"
LIB = REPO_ROOT / "scripts" / "lib" / "ci-skip-token.sh"

# 拼出来，别写字面量（见模块 docstring）。
TOKEN = "[" + "skip ci" + "]"
TOKEN_ALT = "[" + "ci skip" + "]"
TOKEN_UPPER = "[" + "SKIP CI" + "]"


@pytest.fixture(autouse=True)
def _isolate_git_config(tmp_path, monkeypatch):
    """🔴 **本文件每一条都必须与开发者的全局 git 配置隔离**（CI-800 review 实测）。

    `install.sh` 是**故意**认 `core.hooksPath` 的。于是在任何设了全局 hooksPath 的
    机器上（husky / 公司模板），这些用例会把符号链接写进**那个共享目录**，指向
    pytest 的 `tmp_path`；`tmp_path` 被清掉之后它们变成悬空链接，而**悬空链接下
    git 静默跳过钩子** —— 那台机器上的**每一个仓**从此没有钩子，且没有任何提示。
    实测复现过：设了 `core.hooksPath` 之后跑本文件，全局目录里多出 17 个悬空链接。

    ⇒ 把 GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM 指到临时文件。
    变异：去掉本 fixture 并设一个全局 `core.hooksPath`，会有多条用例红，
    且那个全局目录里会出现指向 tmp 的链接。
    """
    cfg = tmp_path / "gitconfig-isolated"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(cfg))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    yield


def _seeded_repo(tmp_path, hooks=("commit-msg", "install.sh")):
    """一个装好钩子的真仓。🔴 端到端用例必须走真 `git commit` —— 路径解析、
    符号链接、cleanup 时机只有在这条路径上才是真的。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True)
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "ci-skip-token.sh").write_text(LIB.read_text())
    for h in hooks:
        src = REPO_ROOT / "scripts" / "hooks" / h
        (repo / "scripts" / "hooks" / h).write_text(src.read_text())
    subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "seed.txt").write_text("x")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True,
                   capture_output=True)
    return repo


def _run_hook(tmp_path, message, hook=None):
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(message)
    return subprocess.run(["bash", str(hook or HOOK), str(f)],
                          capture_output=True, text=True)


# ── 移植落地了没有（少了这条，下面每一条都在测一个不存在的东西）─────────

def test_the_three_files_actually_landed_in_this_repo():
    """🔴 阳性对照。本文件的每一条都读这三个文件；它们缺一个，pytest 报的会是
    `FileNotFoundError`（看起来像用例坏了），而真相是**这道闸压根没移植进来**。
    先显式说清楚。"""
    missing = [str(p.relative_to(REPO_ROOT)) for p in (HOOK, INSTALL, LIB) if not p.exists()]
    assert not missing, f"这道闸没在本仓落地，缺：{missing}"


# ── 该拦的一侧（失效方向：漏放＝整轮 CI 静默消失＝静默不发布）───────────

@pytest.mark.parametrize("tok", [TOKEN, TOKEN_ALT, TOKEN_UPPER])
def test_a_token_in_the_body_is_refused(tmp_path, tok):
    """事故形态。三种写法都要拦 —— 🔴 大小写那条是实测过的：一个键位（大写）
    就能绕过只认小写的守卫。"""
    r = _run_hook(tmp_path, f"CI-999: 修一个东西\n\n判据不是「非 {tok} 提交数为 0」。\n")
    assert r.returncode != 0, f"正文里的 {tok} 被放过去了：\n{r.stdout + r.stderr}"
    assert "正文" in (r.stdout + r.stderr)


def test_the_refusal_names_both_ways_out(tmp_path):
    """🔴 只说「不许」的守卫会被 --no-verify 绕过。两条出路都要说出来，
    因为**它们是不同的两件事**：真想跳过 ⇒ 挪到标题；只是在谈论 ⇒ 换写法。"""
    r = _run_hook(tmp_path, f"CI-999: 修一个东西\n\n那条是 {TOKEN} 的文档提交。\n")
    out = r.stdout + r.stderr
    assert "标题" in out and "换个写法" in out, out


# ── 不该拦的一侧（失效方向：误伤 ⇒ 大家学会 --no-verify，闸当天就死）──

def test_a_token_in_the_subject_is_allowed(tmp_path):
    """合法跳过就长这样。🔴 少了这条，把判据放宽成「整条 message 有没有令牌」
    也能全绿，而那会拦掉每一次合法的跳过提交。"""
    r = _run_hook(tmp_path, f"chore(docs): 只改文档 {TOKEN}\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_token_in_both_subject_and_body_is_allowed(tmp_path):
    """标题里已经有活令牌 ⇒ 正文再提一次不改变任何事，别拦。"""
    r = _run_hook(tmp_path, f"chore(docs): 只改文档 {TOKEN}\n\n（正文也提了 {TOKEN}）\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_ordinary_message_is_allowed(tmp_path):
    r = _run_hook(tmp_path, "CI-999: 一条普通提交\n\n正文里没有任何令牌。\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_token_on_a_top_level_comment_line_is_still_refused(tmp_path):
    """🔴 **这条曾经是反的，而反的那个版本把一个真实的绕过钉成了契约**（CI-800 review）。

    以前认为「`#` 开头的行 git 会删掉 ⇒ 不算数」。**只有编辑器路径成立**：
    `--cleanup=default` 只在要打开编辑器时等于 `strip`，走 `-m`/`-F`/`merge --no-edit`
    时等于 `whitespace`，**顶格 `#` 原样留在最终 message 里**。
    ⇒ 钩子从钩子里看不出这次是哪条路径，只能按「不删」算（误红可见、误绿静默）。

    端到端的证据在 `test_a_commented_token_really_lands_in_the_message`。
    变异：把归一化改回对整条 message 做 `stripspace --strip-comments`，本条必红。
    """
    r = _run_hook(tmp_path, f"CI-999: 普通提交\n\n# 提醒：别在正文里写 {TOKEN}\n")
    assert r.returncode != 0, (
        f"顶格注释行里的令牌被放过去了 —— `-m` 路径下它会真的进 message："
        f"\n{r.stdout + r.stderr}")


# ── 守卫自己坏掉时必须硬失败，而不是安静放行 ──────────────────────────

def test_a_missing_token_lib_refuses_instead_of_passing(tmp_path):
    """🔴 最危险的那条：lib 加载不上时，`if ! _has_ci_skip_token …` 会因为
    「命令不存在」返回 127 而被读成「没有令牌」⇒ **守卫静默消失**，
    而那与「这条 message 确实干净」完全同形。所以必须硬失败。

    变异：把钩子里的 `[ -r "$_lib" ]` / `_assert_ci_skip_token_lib` 去掉，本条必红
    （而且会红成 returncode 0 —— 正是「安静放行」）。
    """
    fake = tmp_path / "scripts" / "hooks"
    fake.mkdir(parents=True)
    (fake / "commit-msg").write_text(HOOK.read_text())      # lib 目录**故意不建**
    r = _run_hook(tmp_path, f"CI-999: 提交\n\n正文里有 {TOKEN}。\n",
                  hook=fake / "commit-msg")
    assert r.returncode != 0, (
        f"lib 缺失时安静放行了 —— 守卫等于不存在：\n{r.stdout + r.stderr}")
    assert "拒绝" in (r.stdout + r.stderr)


# ── 令牌清单只有一份 ──────────────────────────────────────────────────

def test_the_token_list_has_exactly_one_definition(tmp_path):
    """🔴 在 msds-chain 上，这份清单曾经在 `promote-prod.sh` 和 `pre-push` 里各有
    第二份拷贝。两份清单的漂移**不报错**，只会让某一处漏放一种写法 —— 而漏放的
    那一侧正是「CI 静默不跑」。

    判据不是文本比对（那会被注释里的样本骗），而是**行为**：让 lib 对整批写法
    给出判定，并打印它到底判成了什么。
    变异：把 lib 里的清单删掉一条（比如 `[` + `no ci` + `]`），对应那个输入必红。
    """
    cases = [TOKEN, TOKEN_ALT, TOKEN_UPPER,
             "[" + "skip actions" + "]", "[" + "actions skip" + "]",
             "[" + "no ci" + "]", "skip-checks: " + "true",
             "干净的一句话", "skip-ci 这样写是安全的"]
    probe = tmp_path / "probe.sh"
    probe.write_text(
        f'#!/usr/bin/env bash\nset -euo pipefail\n. "{LIB}"\n'
        '_assert_ci_skip_token_lib\n'
        'for s in "$@"; do if _has_ci_skip_token "$s"; then echo HIT; else echo MISS; fi; done\n')
    r = subprocess.run(["bash", str(probe), *cases], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    verdicts = r.stdout.split()
    # 🔴 打印「它到底判成了什么」而不是只断言红绿 —— 红绿判不出「它是不是因为
    #    我想防的那个原因绿的」。
    assert verdicts == ["HIT"] * 7 + ["MISS", "MISS"], list(zip(cases, verdicts))

    # 🔴 **判据要扫目录，别点名文件**：点名一个文件时，同目录里长出的第二份清单
    #    永远不会被发现（msds-chain 上就是这么漏掉 `pre-push` 里那份的）。
    #    ⇒ 扫 `scripts/` 全部，新加脚本自动被纳入，不用回来改这条。
    scanned = sorted(
        q for q in (REPO_ROOT / "scripts").rglob("*")
        if q.is_file() and q.suffix not in (".md", ".pyc")
        and "__pycache__" not in q.parts)
    assert len(scanned) >= 3, f"扫到的文件太少，这条守卫多半没在工作：{scanned}"

    definers, users = [], []
    for q in scanned:
        try:
            src = q.read_text()
        except (UnicodeDecodeError, OSError):
            continue          # 二进制/不可读的，本条不关心
        if "_has_ci_skip_token() {" in src:
            definers.append(q)
        if "_has_ci_skip_token" in src:
            users.append(q)
            assert "ci-skip-token.sh" in src, f"{q.name} 用了令牌判定却没 source 那份 lib"

    assert [q.name for q in definers] == ["ci-skip-token.sh"], (
        f"令牌清单不止一份了：{[str(q.relative_to(REPO_ROOT)) for q in definers]}")
    # 阳性对照：至少得有人在用它，否则上面那条「只有一份定义」是空跑的。
    assert users, "scripts/ 下没有任何脚本用 _has_ci_skip_token —— 这条守卫在空跑"


# ── 安装器 ────────────────────────────────────────────────────────────

def test_the_installer_finds_hooks_without_a_hardcoded_list(tmp_path):
    """🔴 安装器必须**自己发现成员**：'README 里列着装哪几个' 是会腐化的清单，
    而漏装的新钩子与「装了但没触发」完全同形。

    变异（本条要防的那件事）：往 `scripts/hooks/` 放一个新钩子却不改任何清单，
    安装器仍应装上它。这里就是这么测的。
    """
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    hooks_src = repo / "scripts" / "hooks"
    hooks_src.mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "ci-skip-token.sh").write_text(LIB.read_text())
    (hooks_src / "commit-msg").write_text(HOOK.read_text())
    (hooks_src / "README.md").write_text("不是钩子，别装它\n")
    # 「新加的成员」——任何清单都不认识它
    (hooks_src / "pre-commit").write_text("#!/usr/bin/env bash\nexit 0\n")
    # 🔴 **行首/行尾那两个也要在**：安装器的名字清单是多行字符串，第一版按
    #    `*" $name "*` 匹配 ⇒ 位于行首/行尾的名字一侧是换行、**静默漏掉**。
    #    第一版只放了 `pre-commit`（恰好在行中）⇒ 用例全绿而规则是坏的。
    (hooks_src / "pre-push").write_text("#!/usr/bin/env bash\nexit 0\n")
    (hooks_src / "prepare-commit-msg").write_text("#!/usr/bin/env bash\nexit 0\n")
    (hooks_src / "install.sh").write_text(INSTALL.read_text())

    r = subprocess.run(["bash", str(hooks_src / "install.sh")],
                       cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    installed = {p.name for p in (repo / ".git" / "hooks").iterdir() if p.is_symlink()}
    want = {"commit-msg", "pre-commit", "pre-push", "prepare-commit-msg"}
    assert want <= installed, f"漏装了：{want - installed}（装上的：{installed}）\n{r.stdout}"
    assert "README.md" not in installed, "把 README 当钩子装了"

    # --check 在装好之后应当是绿的（阳性对照：否则上面那句「装上了」不构成证据）
    r2 = subprocess.run(["bash", str(hooks_src / "install.sh"), "--check"],
                        cwd=repo, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr


def test_every_name_in_the_installer_list_is_actually_matched(tmp_path):
    """🔴 **判据不许手挑样本**：上一条用例第一版只放了 `pre-commit`，而它恰好在
    清单的行中 ⇒ 用例全绿，可位于行首/行尾的名字其实一个都匹配不上。
    **空跑的是样本不是规则。**

    这里把清单从 `install.sh` 里读出来，**每一个名字都造一个文件**，要求全部装上。
    ⇒ 往清单里加名字不会让这条用例过期，改坏匹配一定红。
    变异：把 `install.sh` 里那句 `| tr '\\n' ' '` 去掉（退回多行匹配），本条必红，
    且报错会**点名说出漏了哪几个**。
    """
    import re
    src = INSTALL.read_text()
    m = re.search(r'_GIT_HOOK_NAMES="\$\(printf \'%s\' "(.*?)" \|', src, re.S)
    assert m, "读不出安装器的钩子名清单 —— 这条用例什么都没测到"
    names = m.group(1).split()
    assert len(names) >= 15, names

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    hooks_src = repo / "scripts" / "hooks"
    hooks_src.mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "ci-skip-token.sh").write_text(LIB.read_text())
    (hooks_src / "install.sh").write_text(INSTALL.read_text())
    for n in names:
        (hooks_src / n).write_text("#!/usr/bin/env bash\nexit 0\n")

    r = subprocess.run(["bash", str(hooks_src / "install.sh")],
                       cwd=repo, capture_output=True, text=True)
    installed = {p.name for p in (repo / ".git" / "hooks").iterdir() if p.is_symlink()}
    missing = sorted(set(names) - installed)
    assert not missing, f"清单里这些名字没被认出来：{missing}\n{r.stdout}\n{r.stderr}"


def test_the_installer_refuses_to_run_from_a_linked_worktree(tmp_path):
    """🔴 `.git/hooks` 是**所有 worktree 共用的一份**，而安装器建的是指向「本次
    checkout 路径」的符号链接 ⇒ 从 worktree 里装，等这个 worktree 被删掉，
    **主 checkout 和别人的每一个 worktree 提交时都会撞上悬空钩子**。
    """
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True)
    hooks_src = repo / "scripts" / "hooks"
    hooks_src.mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "ci-skip-token.sh").write_text(LIB.read_text())
    (hooks_src / "commit-msg").write_text(HOOK.read_text())
    (hooks_src / "install.sh").write_text(INSTALL.read_text())
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True,
                   capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "side"],
                   cwd=repo, check=True, capture_output=True)

    r = subprocess.run(["bash", "scripts/hooks/install.sh"],
                       cwd=wt, capture_output=True, text=True)
    assert r.returncode != 0, f"从 worktree 里装成功了：\n{r.stdout + r.stderr}"
    assert "worktree" in (r.stdout + r.stderr)
    hooks = repo / ".git" / "hooks"
    assert not (hooks / "commit-msg").exists(), "还是装上了 —— 链接会随 worktree 一起悬空"

    # 阳性对照：从主 checkout 装就该成功。少了它，把 install.sh 改成「永远拒绝」也全绿。
    r2 = subprocess.run(["bash", "scripts/hooks/install.sh"],
                        cwd=repo, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert (hooks / "commit-msg").is_symlink(), r2.stdout


def test_the_installer_works_from_a_subdirectory_of_the_main_checkout(tmp_path):
    """🔴 CI-800 review 实测：`git rev-parse --git-dir` / `--git-common-dir` 返回的
    **形式随 cwd 变** —— 仓根是 `.git` / `.git`，子目录里却是 `/abs/…/.git` / `../.git`。
    直接字符串比 ⇒ 在**主 checkout 的任何子目录**里都误判成 linked worktree 并退 1，
    而本脚本的用法说明恰恰写着「在仓根下，或给它绝对路径」。
    `--check` 被当卫生闸用时，它会给出一个**理由完全错误**的非零退出。

    变异：把 `_abs_dir` 那两行换回裸 `git rev-parse` 字符串比较，本条必红。
    """
    repo = _seeded_repo(tmp_path)
    sub = repo / "scripts"
    r = subprocess.run(["bash", "hooks/install.sh", "--check"], cwd=sub,
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert "linked worktree" not in out and "worktree" not in out, (
        f"在主 checkout 的子目录里被误判成 worktree：\n{out}")
    assert r.returncode == 0, f"从子目录跑 --check 退了非零：\n{out}"


def test_a_stale_copy_is_upgraded_even_from_a_subdirectory(tmp_path):
    """🔴 CI-800 review 实测的第二条 pathspec 坑：判「这是不是我们自己的历史版本」
    用的 `git log -- "scripts/hooks/X"` 是 **cwd 相对**的，而同一段里的
    `git rev-parse "$rev:scripts/hooks/X"` 是**根相对**的。两者不同形 ⇒ 从子目录跑时
    `git log` 命中 0 条、`_ours` 恒为 0，**升级旧拷贝那一支永远不执行**，
    旧拷贝被当成别人的钩子留在原地，再也拿不到任何修复。

    变异：把 `:(top)` 去掉，本条必红（会报「不是本仓任何历史版本 —— 没动它」）。
    """
    repo = _seeded_repo(tmp_path)
    old_text = HOOK.read_text()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "hook: 旧版本", "--no-verify"],
                   cwd=repo, check=True, capture_output=True)
    (repo / "scripts" / "hooks" / "commit-msg").write_text(old_text + "\n# 新版本多的一行\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "hook: 新版本", "--no-verify"],
                   cwd=repo, check=True, capture_output=True)
    hooks = repo / ".git" / "hooks"
    (hooks / "commit-msg").unlink(missing_ok=True)
    (hooks / "commit-msg").write_text(old_text)
    (hooks / "commit-msg").chmod(0o755)

    r = subprocess.run(["bash", "hooks/install.sh"], cwd=repo / "scripts",
                       capture_output=True, text=True)
    assert r.returncode == 0, f"从子目录跑失败了：\n{r.stdout + r.stderr}"
    assert (hooks / "commit-msg").is_symlink(), (
        f"旧拷贝没被认出来（pathspec 又变回 cwd 相对了？）：\n{r.stdout + r.stderr}")


def test_a_non_executable_hook_is_not_reported_as_installed(tmp_path):
    """🔴 CI-800 review 实测：git 对**不可执行**的钩子是静默跳过的
    （`advice.ignoredHook` 那句只是 hint，还能被关掉）⇒ 链接建得好好的、
    `--check` 报 ✅ 退 0，而带令牌的提交照样过 —— 正是本脚本存在要排除的
    「装了但没触发」。判据必须是「链接对 **且** 目标可执行」。

    🔴 exec 位特别容易丢：`git add` 会按工作树重读它。
    变异：把 `[ ! -x "$_src" ]` 那一段去掉，本条必红（`--check` 会报 ✅ 退 0）。
    """
    repo = _seeded_repo(tmp_path)
    src = repo / "scripts" / "hooks" / "commit-msg"
    src.chmod(0o644)

    # ① 前提：不可执行时钩子确实不触发（否则本条什么都没测到）
    leaked = subprocess.run(["git", "commit", "--allow-empty", "-m", "CI-999: t",
                             "-m", f"正文里有 {TOKEN}"],
                            cwd=repo, capture_output=True, text=True)
    assert leaked.returncode == 0, "前提不成立：不可执行的钩子竟然拦住了"

    # ② --check 必须报出来，不能报绿
    chk = subprocess.run(["bash", "scripts/hooks/install.sh", "--check"], cwd=repo,
                         capture_output=True, text=True)
    assert chk.returncode != 0, (
        f"源文件不可执行，--check 却报绿 —— 这正是「装了但没触发」：\n{chk.stdout + chk.stderr}")

    # ③ 装一次应当把它修好，并且真的拦得住
    fix = subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo,
                         capture_output=True, text=True)
    assert fix.returncode == 0, fix.stdout + fix.stderr
    assert os.access(src, os.X_OK), "exec 位没被补上"
    after = subprocess.run(["git", "commit", "--allow-empty", "-m", "CI-999: t2",
                            "-m", f"正文里有 {TOKEN}"],
                           cwd=repo, capture_output=True, text=True)
    assert after.returncode != 0, f"修完仍不触发：\n{after.stdout + after.stderr}"


def test_an_unreadable_message_file_refuses_instead_of_passing(tmp_path):
    """🔴 CI-800 review 的 LOW，但方向是**开着的**：`set -uo pipefail` 没有 `-e`，
    归一化用的 `awk` 失败时 `_cut` 是空串 ⇒ subject/body 都空 ⇒ 一路走到 exit 0。
    **闸在自己坏掉的时候放行**，与 lib 加载不上那条同族。
    变异：把 `if ! _cut=…` 那个判断换回裸赋值，本条必红（会退 0）。
    """
    r = subprocess.run(["bash", str(HOOK), str(tmp_path / "does-not-exist")],
                       capture_output=True, text=True)
    assert r.returncode != 0, (
        f"读不到 message 文件时安静放行了：\n{r.stdout + r.stderr}")


def test_the_installer_does_not_overwrite_someone_elses_hook(tmp_path):
    """别人（或别的工具）装过的钩子不许被悄悄换掉 —— 换掉之后它不报错，只是不再拦人。"""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    hooks_src = repo / "scripts" / "hooks"
    hooks_src.mkdir(parents=True)
    (hooks_src / "commit-msg").write_text(HOOK.read_text())
    (hooks_src / "install.sh").write_text(INSTALL.read_text())
    theirs = repo / ".git" / "hooks" / "commit-msg"
    theirs.parent.mkdir(parents=True, exist_ok=True)
    theirs.write_text("#!/bin/sh\n# 别人的钩子\nexit 0\n")

    r = subprocess.run(["bash", str(hooks_src / "install.sh")],
                       cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0, "覆盖了别人的钩子还报成功"
    assert "别人的钩子" in theirs.read_text(), "别人的钩子被覆盖了"


def test_the_installer_honours_core_hookspath(tmp_path):
    """🔴 设了 `core.hooksPath` 时 git 从别处读钩子，而第一版照样往 `.git/hooks`
    建链接、打印 ✅、`--check` 也退 0 ⇒ **「装了但没触发」**。
    变异：把 `--git-path hooks` 换回 `$(--git-common-dir)/hooks`，本条必红。
    """
    repo = _seeded_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "config", "core.hooksPath", str(elsewhere)],
                   cwd=repo, check=True)
    r = subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (elsewhere / "commit-msg").is_symlink(), (
        f"装到了 .git/hooks 而 git 根本不看那里：\n{r.stdout}")
    # 真的会触发吗（判据不是「链接建出来了」，是「钩子拦得住」）
    bad = subprocess.run(["git", "commit", "--allow-empty", "-m", "CI-999: t",
                          "-m", f"正文里有 {TOKEN}"],
                         cwd=repo, capture_output=True, text=True)
    assert bad.returncode != 0, f"钩子没触发：\n{bad.stdout + bad.stderr}"


def test_a_dangling_hook_symlink_is_healed(tmp_path):
    """🔴 悬空链接下 git **静默跳过钩子** —— 不报错、不警告，守卫就没了。
    而这正是「从 worktree 装钩子、worktree 被删」留下的残骸。
    变异：把 `[ -L ] && [ ! -e ]` 那一支去掉，本条必红。
    """
    repo = _seeded_repo(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    (gone / "commit-msg").write_text(HOOK.read_text())
    dst = repo / ".git" / "hooks" / "commit-msg"
    dst.unlink(missing_ok=True)
    dst.symlink_to(gone / "commit-msg")
    import shutil
    shutil.rmtree(gone)
    assert dst.is_symlink() and not dst.exists(), "没造出悬空链接，这条用例什么都没测到"

    # 先确认「悬空 ⇒ 守卫静默消失」这个前提是真的
    leaked = subprocess.run(["git", "commit", "--allow-empty", "-m", "CI-999: t",
                             "-m", f"正文里有 {TOKEN}"],
                            cwd=repo, capture_output=True, text=True)
    assert leaked.returncode == 0, "前提不成立：悬空链接下 git 竟然拦住了"

    r = subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"悬空链接没被修好：\n{r.stdout + r.stderr}"
    assert "no such file" not in (r.stdout + r.stderr), (
        f"往 stderr 喷了 cd 的报错：\n{r.stderr}")
    after = subprocess.run(["git", "commit", "--allow-empty", "-m", "CI-999: t2",
                            "-m", f"正文里有 {TOKEN}"],
                           cwd=repo, capture_output=True, text=True)
    assert after.returncode != 0, f"修完仍不触发：\n{after.stdout + after.stderr}"


def test_a_stale_copy_of_our_own_hook_is_upgraded_to_a_link(tmp_path):
    """🔴 照着拷贝过去的人会永远停在拷贝那天的版本 —— 钩子还在、还会跑、只是
    **再也拿不到任何修复**，而这与「装的是最新版」完全同形。**修了但没到达真正
    的消费者。** 判据必须**可推导**：blob hash 对上本仓该文件的任一历史版本。
    变异：把那一支去掉，本条必红（旧拷贝会被当成别人的钩子，永远不升级）。
    """
    repo = _seeded_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    old_text = HOOK.read_text()
    # 🔴 先把「旧版本」**提交进历史** —— 判据是 blob hash 对上本仓某个历史版本，
    #    只在工作树里放一份是不算数的。
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "hook: 旧版本", "--no-verify"],
                   cwd=repo, check=True, capture_output=True)
    (repo / "scripts" / "hooks" / "commit-msg").write_text(old_text + "\n# 新版本多的一行\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "hook: 新版本", "--no-verify"],
                   cwd=repo, check=True, capture_output=True)
    (hooks / "commit-msg").unlink(missing_ok=True)
    (hooks / "commit-msg").write_text(old_text)          # ← 旧版的**拷贝**，不是链接
    (hooks / "commit-msg").chmod(0o755)

    r = subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"旧拷贝没被升级：\n{r.stdout + r.stderr}"
    assert (hooks / "commit-msg").is_symlink(), (
        f"仍是拷贝 —— 它拿不到任何后续修复：\n{r.stdout + r.stderr}")


def test_a_foreign_hook_file_is_still_left_alone(tmp_path):
    """阳性对照 / 反方向：**不是**本仓任何历史版本的文件绝不能被换掉。
    少了这条，把上一条实现成「见到普通文件就覆盖」也能全绿，而那会悄悄干掉
    别人（或别的工具）装的钩子 —— 比不装更糟。"""
    repo = _seeded_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    (hooks / "commit-msg").unlink(missing_ok=True)
    (hooks / "commit-msg").write_text("#!/bin/sh\n# 别人的钩子\nexit 0\n")

    r = subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo,
                       capture_output=True, text=True)
    assert r.returncode != 0, "覆盖了别人的钩子还报成功"
    assert "别人的钩子" in (hooks / "commit-msg").read_text(), "别人的钩子被覆盖了"


# ── 端到端：真的 git commit 走一遍（票里写死的那条判据）───────────────

def test_a_fresh_clone_that_ran_the_installer_actually_blocks(tmp_path):
    """🔴 **CI-800 的验收判据**：不是「文件在仓里」，是「**新克隆跑一次
    `install.sh` 之后真的拦得住**」。钩子不受版本控制 ⇒ 文件躺在仓里而没人装时，
    这道闸完全不存在，且仓看起来和装好了一模一样。

    这里造一个全新的仓 → 拷进三个文件 → 跑一次 `install.sh` → 真的 `git commit`。
    前半段：事故形态被拦且**没有提交产生**。后半段：改掉写法就能提交（否则
    被误伤的人学会 `--no-verify`，闸当天就死）。

    🔴 走真的 `git commit` 而不是直接调钩子：钩子拿到的是 git 写的那份文件，
    路径解析、符号链接、cleanup 时机都只有在这条路径上才是真的。
    """
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True)
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "ci-skip-token.sh").write_text(LIB.read_text())
    (repo / "scripts" / "hooks" / "commit-msg").write_text(HOOK.read_text())
    (repo / "scripts" / "hooks" / "install.sh").write_text(INSTALL.read_text())

    # 阳性对照：**装之前**这道闸不存在。少了它，「装完拦住了」不构成证据
    # ——一个恒拦的钩子也能让下面那半绿。
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    before = subprocess.run(
        ["git", "commit", "-m", "CI-999: 装之前", "-m", f"正文里有 {TOKEN}"],
        cwd=repo, capture_output=True, text=True)
    assert before.returncode == 0, (
        "装之前就被拦了 —— 那说明拦住它的不是本仓这道闸，本条用例什么都没证明："
        f"\n{before.stdout + before.stderr}")

    subprocess.run(["bash", "scripts/hooks/install.sh"], cwd=repo, check=True,
                   capture_output=True)

    (repo / "b.txt").write_text("x")
    subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                 capture_output=True, text=True).stdout.strip()
    bad = subprocess.run(
        ["git", "commit", "-m", "CI-999: 修东西", "-m", f"判据不是「非 {TOKEN} 提交数为 0」"],
        cwd=repo, capture_output=True, text=True)
    assert bad.returncode != 0, f"真 commit 没被拦：\n{bad.stdout + bad.stderr}"
    assert "正文" in (bad.stdout + bad.stderr)
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
    assert head_after == head_before, "被拦了却还是提交了"

    ok = subprocess.run(
        ["git", "commit", "-m", "CI-999: 修东西", "-m", "判据不是「非跳过令牌提交数为 0」"],
        cwd=repo, capture_output=True, text=True)
    assert ok.returncode == 0, f"换掉写法之后仍被拦：\n{ok.stdout + ok.stderr}"


def test_a_verbose_commit_does_not_choke_on_the_staged_diff(tmp_path):
    """🔴 `git commit -v` / `commit.verbose=true` 把**暂存的整个 diff** 附在剪刀线
    之下，那些行**不带注释前缀** ⇒ 第一版的 `grep -v '^#'` 留下了它们。
    后果很具体：**改本仓 `scripts/lib/ci-skip-token.sh` 时必触发**（它内容里就有
    令牌字面量），于是一条完全干净的 message 被拦，而被误伤的人学会的是
    `--no-verify` —— 闸当天就死。

    🔴 用**编辑器路径**复现：`-m` 不会附 diff，所以拿 `-m` 测这条**什么都测不到**。
    变异：把钩子里的剪刀线截断去掉，本条必红。
    """
    repo = _seeded_repo(tmp_path)
    (repo / "tokenfile.txt").write_text("TOK=" + TOKEN + "\n")
    subprocess.run(["git", "add", "tokenfile.txt"], cwd=repo, check=True)

    ed = tmp_path / "ed.sh"
    ed.write_text("#!/bin/sh\n"
                  "printf 'CI-999: 干净的标题\\n\\n干净的正文，一个令牌都没有\\n' > \"$1.new\"\n"
                  "cat \"$1\" >> \"$1.new\" && mv \"$1.new\" \"$1\"\n")
    ed.chmod(0o755)
    env = dict(os.environ, GIT_EDITOR=str(ed))
    r = subprocess.run(["git", "commit", "-v"], cwd=repo, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        f"干净的 message 被暂存 diff 里的令牌拦了：\n{r.stdout + r.stderr}")
    subj = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    assert subj == "CI-999: 干净的标题", subj


def test_an_indented_comment_line_is_still_part_of_the_message(tmp_path):
    """🔴 **假阴性**——本钩子存在的全部理由的反面。git 只把**顶格**的注释字符当
    注释；第一版 `^[[:space:]]*#` 把缩进的也删了 ⇒ 正文里「   # …令牌…」的提交
    被放行，最终 message 里令牌活着，整轮 CI 静默不跑。
    变异：把归一化换回 `grep -v '^[[:space:]]*#'`，本条必红。
    """
    repo = _seeded_repo(tmp_path)
    r = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "CI-999: t",
         "-m", f"   # 上一条是 {TOKEN} 的文档提交"],
        cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0, (
        "缩进的注释行里的令牌被放过去了 —— 它会进最终 message 并关掉整轮 CI："
        f"\n{r.stdout + r.stderr}")
    body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo,
                          capture_output=True, text=True).stdout
    assert "999" not in body, f"被拦了却还是提交了：{body}"


def test_a_commented_token_really_lands_in_the_message(tmp_path):
    """🔴 **CI-800 review 抓到的 HIGH，端到端的那一半。**

    这条用例存在的理由是：上一版在这里断言 `returncode == 0` 且**从不检查
    `%B`** —— 于是「钩子放行了」和「令牌没进 message」被当成一回事，而它们不是。
    真相是 `-m` 路径下 git 不删顶格注释，令牌**真的进了最终 message**，
    整轮 CI 静默不跑；本仓 push main 即部署 ⇒ 静默不发布。

    判据分两段写清楚：① 钩子拦住 ② 假如没拦住，令牌确实会留在 `%B` 里。
    ②那半用 `--no-verify` 绕过钩子来证明前提为真——**没有它，①就只是一个
    自说自话的断言**。
    """
    repo = _seeded_repo(tmp_path)

    # ② 先证明前提：绕过钩子提交，令牌确实活在最终 message 里
    subprocess.run(["git", "commit", "--allow-empty", "--no-verify", "-m", "CI-999: 前提",
                    "-m", f"# 谈论 {TOKEN} 这个令牌"],
                   cwd=repo, check=True, capture_output=True)
    body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo,
                          capture_output=True, text=True).stdout
    assert TOKEN in body, (
        "前提不成立：这个 git 版本在 -m 路径下删掉了顶格注释。"
        f"若真如此，本条要防的绕过就不存在了，请重判。实际 %B：\n{body}")

    # ① 钩子必须拦住它
    r = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "CI-999: t",
         "-m", f"# 提醒：别在正文里写 {TOKEN}"],
        cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0, (
        "顶格注释里的令牌被放过去了，而上面刚证明它会真的进 message ⇒ 整轮 CI 静默不跑："
        f"\n{r.stdout + r.stderr}")


def test_the_editor_path_with_a_clean_message_is_not_touched(tmp_path):
    """阳性对照 / 误报侧：改成「注释一律算数」之后，**编辑器路径下 git 自己写的
    那一大块注释**（`# Please enter the commit message…` / `-v` 的文件清单）
    绝不能把干净的提交拦下来。少了这条，把判据改成「整条 message 有没有令牌」
    也能让上面几条绿，而那会让每一次带模板的提交都被拦。
    """
    repo = _seeded_repo(tmp_path)
    (repo / "c.txt").write_text("x")
    subprocess.run(["git", "add", "c.txt"], cwd=repo, check=True)
    ed = tmp_path / "ed2.sh"
    ed.write_text("#!/bin/sh\n"
                  "printf 'CI-999: 干净的标题\\n\\n干净的正文\\n' > \"$1.new\"\n"
                  "cat \"$1\" >> \"$1.new\" && mv \"$1.new\" \"$1\"\n")
    ed.chmod(0o755)
    r = subprocess.run(["git", "commit"], cwd=repo,
                       env=dict(os.environ, GIT_EDITOR=str(ed)),
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        f"编辑器路径下干净的提交被自己的模板注释拦了：\n{r.stdout + r.stderr}")
