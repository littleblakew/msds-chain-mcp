#!/usr/bin/env bash
# CI 跳过令牌 —— **本仓唯一一份清单**（CI-685）。别在任何地方再抄一份。
#
# GitHub Actions 按 commit message 里有没有这些**字面量**决定跳不跳过整轮 workflow，
# 它不管你是在「用」它还是在「谈论」它。这条已经害过四次（前三次在 msds-chain）：
#   · 2026-08-20  promote 的 --override 理由里描述别的 commit ⇒ main 收到代码却零 workflow
#   · 2026-08-23  commit body 里引用它解释「本文件为它记过坑」⇒ 零 workflow，push 一切正常
#   · 2026-08-31  改 promote-prod.sh 时在 commit body 里引用它 ⇒ 同上（**而该文件顶部
#                 就写着这个坑，写的人读过、写过、仍然犯**）
#   · 2026-08-31  msds-chain-gateway：merge commit 正文里解释「为什么这次不加那个令牌」
#                 ⇒ 整轮 CI 静默不跑，**而该仓 push main 即部署且没有 promote gate**
#                 ⇒ 静默不跑＝**静默不发布**，Prod 镜像停在上一版。发现它只能靠
#                 「main 变了但镜像 tag 没变」——这一次正是本仓这份钩子的由来（CI-800）。
#
# 🔴 大小写不敏感：GitHub 那边匹配不分大小写，而 shell 的 `case` 分。review 实测过
#    `[skip ci]` 被拦、`[SKIP CI]` 直接走过去 —— **一个键位就绕过整道守卫**。
#    用 `tr` 而不是 `${1,,}`：本机 bash 是 3.2，没有那个展开。
#
# 🔴 **本文件里绝不能出现裸的令牌字面量**（注释里也不行）：任何引用它的地方，
#    这份文件本身都会被读进去比对；更要紧的是，**改本文件的那个 commit 的 message
#    也不能出现它**，否则改「防止误跳过 CI」的提交自己把 CI 跳掉了。
#    上面几行只写方括号里的词，就是为了这个。
#
# 用法：
#   . "<repo>/scripts/lib/ci-skip-token.sh" || exit 1
#   _has_ci_skip_token "<某段文本>" && ...
#   🔴 source 之后**必须断言函数存在**（见 `_assert_ci_skip_token_lib`）：在 `if !` 条件里
#      调用一个不存在的函数返回 127，`!` 把它变成真 ⇒ **静默退化成「从来查不到令牌」**，
#      而那与「这段文本确实干净」完全同形。set -e 在 `if` 条件里也不救你。

# 令牌清单。加一条就只加在这里。
_CI_SKIP_TOKENS='[skip ci]
[ci skip]
[skip actions]
[actions skip]
[no ci]
skip-checks: true'

_has_ci_skip_token() {
  local _hay _tok
  _hay="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  while IFS= read -r _tok; do
    [ -n "$_tok" ] || continue
    case "$_hay" in *"$_tok"*) return 0 ;; esac
  done <<EOF
$_CI_SKIP_TOKENS
EOF
  return 1
}

# 🔴 调用方 source 完立刻调它。理由见上：函数没定义时的失效方向是**关闭的**
#    （查不到令牌＝放行），所以必须显式证明它在。
_assert_ci_skip_token_lib() {
  if ! type _has_ci_skip_token >/dev/null 2>&1; then
    echo "❌ ci-skip-token.sh 没加载成功（_has_ci_skip_token 未定义）—— 拒绝在没有令牌检查的情况下继续。" >&2
    return 1
  fi
  return 0
}
