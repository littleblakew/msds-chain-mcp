#!/usr/bin/env bash
#
# 装上本目录下的**全部**钩子（CI-685）。
#
# 🔴 **自己发现成员，别维护名单**：`.git/hooks/` 不受版本控制，所以钩子必须手工装；
#    而「README 里列着装哪几个」是一份会腐化的清单 —— 加了新钩子却没人回来改那一行，
#    新钩子就永远是个没人装的空文件，**而它和「装了但没触发」完全同形**。
#    这里扫目录，凡是名字是 git 认识的钩子名的普通文件，一律建链接；
#    exec 位不当成筛选条件而是**当成要修的东西**（不可执行的钩子 git 静默跳过）。
#
# 用法（在仓根下，或给它绝对路径）：
#   scripts/hooks/install.sh          # 装
#   scripts/hooks/install.sh --check  # 只报告，不动手（退出码非零＝有没装上的）
#
# 🔴 worktree 共享主仓的 `.git/hooks`（`$GIT_COMMON_DIR/hooks`）⇒ **装一次覆盖所有 worktree**。
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
_check_only=0
[ "${1:-}" = "--check" ] && _check_only=1

# git 认识的钩子名。不在这份里的文件（README.md / install.sh / lib）自然被跳过。
# 🔴 这份清单是 git 定义的、不是我们的策略 ⇒ 它不会因为我们加钩子而过期。
# 🔴 **必须归一化成单行空格分隔再匹配。** 第一版直接对多行字符串做
#    `case " $_GIT_HOOK_NAMES " in *" $_name "*)`——那个模式要求名字**两侧都是空格**，
#    而位于行首/行尾的名字一侧是换行 ⇒ **静默跳过**。实测：`pre-push`（行尾）和
#    `prepare-commit-msg`（行首）都匹配不上，只有行中的名字有效。
#    🔴 抓不到它的原因值得记：用例里我挑的样本 `pre-commit` 恰好在行中 ⇒ 用例全绿，
#    **空跑的是样本不是规则**。所以下面的守卫改成「清单里每一个名字都要被认出来」。
_GIT_HOOK_NAMES="$(printf '%s' "applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit
prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge pre-push
post-update push-to-checkout pre-auto-gc post-rewrite sendemail-validate" | tr '\n' ' ')"

# 把符号链接解析成绝对真身路径。`readlink -f` 在旧 macOS 上没有 ⇒ 自己拼。
# 把符号链接解析成绝对真身路径。`readlink -f` 在旧 macOS 上没有 ⇒ 自己拼。
# 🔴 目标已经不存在（悬空链接）时，那串 `cd` 会失败并**把 `cd: no such file` 喷到 stderr**，
#    看起来像脚本坏了。整段兜住，解析不出来就返回空串，让调用方按「不是我们的链接」处理。
_resolve_link() {
  local _l _t
  _l="$1"; _t="$(readlink "$_l" 2>/dev/null)" || return 0
  case "$_t" in
    /*) printf '%s\n' "$_t" ;;
    *)  printf '%s\n' "$( { cd "$(dirname "$_l")" && cd "$(dirname "$_t")" && pwd -P; } 2>/dev/null)/$(basename "$_t")" ;;
  esac
}

# 🔴 **别从 linked worktree 里装。** `.git/hooks` 是**所有 worktree 共用的一份**，
#    而这里建的是指向「本次 checkout 路径」的符号链接 ⇒ worktree 一删，那个链接就悬空，
#    于是**主 checkout 和别人的每一个 worktree 提交时都会撞上一个跑不起来的钩子**。
#    2026-08-31 差一点这么干（装完才想起来 worktree 待会儿要删）。
# 🔴 **必须先解析成绝对真身再比**（CI-800 review 实测）：`git rev-parse` 返回的形式
#    随 cwd 变——在仓根是 `.git` / `.git`，在**子目录**却是 `/abs/…/.git` / `../.git`。
#    直接字符串比 ⇒ 在**主 checkout 的任何子目录**里都会误判成 linked worktree 并退 1，
#    而本文件的用法说明恰恰写着「在仓根下，**或给它绝对路径**」。
#    不用 `--path-format=absolute`（要 git ≥2.31），自己 cd + pwd -P 更稳。
_abs_dir() { ( cd "$1" 2>/dev/null && pwd -P ); }
_gd="$(_abs_dir "$(git rev-parse --git-dir)")"
_gcd="$(_abs_dir "$(git rev-parse --git-common-dir)")"
if [ -z "$_gd" ] || [ -z "$_gcd" ]; then
  echo "❌ 解析不出 git 目录 —— 拒绝在不知道自己在哪的情况下装钩子。" >&2
  exit 1
fi
if [ "$_gd" != "$_gcd" ] && [ "${FORCE_WORKTREE_INSTALL:-}" != "1" ]; then
  {
    echo "❌ 这是一个 linked worktree，拒绝在这里装钩子。"
    echo "   .git/hooks 是所有 worktree 共用的，而链接会指向本 worktree 的路径"
    echo "   ⇒ 这个 worktree 一删，所有人的提交都会撞上悬空钩子。"
    echo "   ⇒ 回主 checkout 装：cd $(git worktree list --porcelain | sed -n '1s/^worktree //p;1q') && scripts/hooks/install.sh"
    echo "   （确知要这么做：FORCE_WORKTREE_INSTALL=1）"
  } >&2
  exit 1
fi

# 🔴 用 `--git-path hooks` 而不是 `$(--git-common-dir)/hooks`（review 实测，MEDIUM）：
#    设了 `core.hooksPath`（全局配置 / husky / 公司模板）时 git 从**别处**读钩子，
#    而第一版照样往 `.git/hooks` 建链接并打印 ✅、`--check` 也退 0
#    ⇒ **「装了但没触发」**，正是本目录 README 说这套设计必须避免的那个形状。
#    `--git-path` 认 `core.hooksPath`，实测返回的就是它。
_hooks_dir="$(git rev-parse --git-path hooks)"
if _hp="$(git config --get core.hooksPath)" && [ -n "$_hp" ]; then
  echo "ℹ️  core.hooksPath 已设为 $_hp —— 钩子装到那里（不是 .git/hooks）。"
fi
mkdir -p "$_hooks_dir"

_missing=0
_installed=0
for _src in "$_here"/*; do
  _name="$(basename "$_src")"
  case " $_GIT_HOOK_NAMES " in *" $_name "*) ;; *) continue ;; esac
  [ -f "$_src" ] || continue
  _dst="$_hooks_dir/$_name"
  # 🔴 **exec 位要单独判**（CI-800 review 实测）：git 对不可执行的钩子是**静默跳过**的
  #    （`advice.ignoredHook` 那句提示可以被关掉，且它只是 hint 不是错误）⇒ 链接建得
  #    好好的、`--check` 报 ✅ 退 0，而带令牌的提交照样过 —— 正是本脚本存在要排除的
  #    「装了但没触发」。判据因此是「**链接对 + 目标可执行**」，不是「链接对」。
  #    🔴 exec 位还特别容易丢：`git add` 会按工作树重读它，`update-index --chmod=+x`
  #       会被抹回 644（本 workspace 记过这个坑）。
  if [ ! -x "$_src" ]; then
    if [ "$_check_only" = "1" ]; then
      echo "❌ $_name 源文件不可执行（git 会静默跳过它）—— 跑 scripts/hooks/install.sh 修" >&2
      _missing=$((_missing+1)); continue
    fi
    echo "🩹 $_name 源文件不可执行（git 会静默跳过）—— 补上 exec 位"
    chmod +x "$_src"
  fi
  if [ -L "$_dst" ] && [ "$(_resolve_link "$_dst")" = "$_src" ]; then
    echo "✅ $_name 已装"
    _installed=$((_installed+1)); continue
  fi
  # 已经存在、但不是链接 ⇒ 先问「**它是不是我们自己某个历史版本的拷贝**」。
  # 🔴 这一支不是可有可无的：旧 README 教的是 `ln -sf`，但**照着拷贝过去的人**
  #    会永远停在拷贝那天的版本 —— 钩子还在、还会跑、只是**再也拿不到任何修复**，
  #    而这与「装的是最新版」完全同形，没人会发现。2026-08-31 在本仓真撞到：
  #    `.git/hooks/pre-push` 是一份逐字等于旧 tracked 版本的拷贝，比当前版本少 31 行。
  # 🔴 判据是**可推导的**（blob hash 对上本仓该文件的任一历史版本），不是「看着像」。
  #    对不上就当别人的钩子处理 —— 悄悄换掉别人的比不装更糟。
  if [ -e "$_dst" ] && ! [ -L "$_dst" ]; then
    _dst_hash="$(git hash-object "$_dst" 2>/dev/null || true)"
    _ours=0
    if [ -n "$_dst_hash" ]; then
      # 🔴 pathspec 必须锚在仓根（`:(top)`）。第一版是 **cwd 相对**的，而下一行的
      #    `rev-parse "$_rev:scripts/hooks/$_name"` 是**根相对**的 —— 两者不同形
      #    ⇒ 从子目录跑时 `git log` 命中 0 条、`_ours` 恒为 0，于是「把自己的旧拷贝
      #    升级成链接」那一支**永远不执行**，旧拷贝被当成别人的钩子留在原地，
      #    再也拿不到任何修复（正是这一支存在的全部理由）。实测复现过。
      for _rev in $(git log --format=%H -- ":(top)scripts/hooks/$_name" 2>/dev/null); do
        if [ "$(git rev-parse "$_rev:scripts/hooks/$_name" 2>/dev/null || true)" = "$_dst_hash" ]; then
          _ours=1; break
        fi
      done
    fi
    if [ "$_ours" = "1" ]; then
      if [ "$_check_only" = "1" ]; then
        echo "❌ $_name 是本仓旧版本的**拷贝**（拿不到后续修复）—— 跑 scripts/hooks/install.sh 换成链接" >&2
        _missing=$((_missing+1)); continue
      fi
      echo "🩹 $_name 是本仓旧版本的拷贝（拿不到后续修复）—— 换成链接"
      ln -sfn "$_src" "$_dst"
      _installed=$((_installed+1)); continue
    fi
    echo "⚠️  $_name 已存在、不是链接、也不是本仓任何历史版本 —— 没动它，自己看一眼：$_dst" >&2
    _missing=$((_missing+1)); continue
  fi
  # 🔴 **悬空链接必须自愈**（review 实测，MEDIUM）：目标已不存在时 git **静默跳过钩子**
  #    ——不报错、不警告，守卫就这么消失了；而第一版把它归到「指向别处、没动它」并退 1，
  #    **永远修不好**。这种链接只可能是我们自己留下的陈旧的那个，或一个已经坏掉的外来的，
  #    两种情况下换掉它都比留着强。（这正是从 worktree 装钩子会造出的那个残骸。）
  if [ -L "$_dst" ] && [ ! -e "$_dst" ]; then
    if [ "$_check_only" = "1" ]; then
      echo "❌ $_name 是**悬空**链接（git 会静默跳过它）—— 跑 scripts/hooks/install.sh 修" >&2
      _missing=$((_missing+1)); continue
    fi
    echo "🩹 $_name 是悬空链接（指向已不存在的 $(readlink "$_dst" 2>/dev/null)）—— 换成本仓的"
    ln -sfn "$_src" "$_dst"
    _installed=$((_installed+1)); continue
  fi
  if [ -L "$_dst" ]; then
    echo "⚠️  $_name 是指向别处的符号链接（$(readlink "$_dst")）—— 没动它。" >&2
    _missing=$((_missing+1)); continue
  fi
  if [ "$_check_only" = "1" ]; then
    echo "❌ $_name 未装（跑 scripts/hooks/install.sh）" >&2
    _missing=$((_missing+1)); continue
  fi
  ln -sf "$_src" "$_dst"
  chmod +x "$_src"
  echo "🔗 装上 $_name → $_src"
  _installed=$((_installed+1))
done

if [ "$_installed" = "0" ] && [ "$_missing" = "0" ]; then
  echo "⚠️  scripts/hooks/ 下一个 git 钩子都没找到 —— 这多半是脚本坏了，不是真的没有钩子。" >&2
  exit 1
fi
[ "$_missing" = "0" ] || exit 1
exit 0
