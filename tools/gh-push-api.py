#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不用 git 传输、直接拿 GitHub API 把仓库内容推上去。

为什么要这样：国内网络经常连不上 github.com（git push 走的那个域名），
但 api.github.com 是通的。所以走 Git Data API：
  文件 -> blob -> tree -> commit -> 更新 ref

用法：python3 tools/gh-push-api.py <owner/repo> [--branch main] [--dry]
令牌从 ~/.mc-keys/github.token 读。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
TOKEN = open(os.path.expanduser("~/.mc-keys/github.token")).read().strip()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(method, path, payload=None, tries=4):
    for i in range(tries):
        req = urllib.request.Request(
            API + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "token " + TOKEN,
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "mc-seed-toolkit", "Content-Type": "application/json"},
            method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            if e.code in (500, 502, 503) and i + 1 < tries:
                time.sleep(2 * (i + 1))
                continue
            raise SystemExit(f"API {method} {path} -> HTTP {e.code}: {body}")
        except Exception as e:
            if i + 1 < tries:
                time.sleep(2 * (i + 1))
                continue
            raise SystemExit(f"API {method} {path} -> {e}")


def tracked_files():
    """用 git 列出该提交的文件（.gitignore 已经帮我们排除了种子、记录、自带 Java）"""
    # core.quotepath=false：不然中文文件名会被转义成 \344\275\277 那种
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    files = [f for f in out.splitlines() if f.strip()]
    # 光用 ls-files 会漏掉"新加但还没 git add 的文件"—— 上一次就因此把
    # app/onboard.py 漏在仓库外面了。这里把未被忽略的新文件也带上。
    out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files",
                          "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    extra = [f for f in out.splitlines() if f.strip()]
    if extra:
        print(f"（另有 {len(extra)} 个新文件还没 git add，也一起推：" +
              "、".join(extra[:5]) + ("…" if len(extra) > 5 else "") + "）")
    return files + extra


def main():
    repo = sys.argv[1]
    branch = "main"
    if "--branch" in sys.argv:
        branch = sys.argv[sys.argv.index("--branch") + 1]
    dry = "--dry" in sys.argv
    message = None
    for i, a in enumerate(sys.argv):
        if a == "--message" and i + 1 < len(sys.argv):
            message = sys.argv[i + 1]
    if not message:
        try:
            with open(os.path.join(ROOT, "app", "VERSION"), encoding="utf-8") as fh:
                ver = fh.read().strip()
        except OSError:
            ver = "?"
        message = f"MC 种子工具包 v{ver}（源码 + 模组 + 文档 + 测试）"
    files = tracked_files()
    total = sum(os.path.getsize(os.path.join(ROOT, f)) for f in files)
    print(f"要推 {len(files)} 个文件，共 {total/1048576:.1f} MB")
    if dry:
        for f in files[:10]:
            print("  ", f)
        return 0

    # ① 每个文件做成 blob
    tree = []
    for i, rel in enumerate(files, 1):
        with open(os.path.join(ROOT, rel), "rb") as fh:
            data = fh.read()
        res = call("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        tree.append({"path": rel, "mode": "100755" if os.access(
            os.path.join(ROOT, rel), os.X_OK) else "100644",
            "type": "blob", "sha": res["sha"]})
        if i % 20 == 0 or i == len(files):
            print(f"  blob {i}/{len(files)}")

    # ② tree
    res = call("POST", f"/repos/{repo}/git/trees", {"tree": tree})
    tree_sha = res["sha"]
    print("  tree:", tree_sha[:10])

    # ③ commit（有历史就接在它后面）
    head = None
    try:
        head = call("GET", f"/repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
    except SystemExit:
        pass
    payload = {"message": message, "tree": tree_sha}
    if head:
        payload["parents"] = [head]
    commit = call("POST", f"/repos/{repo}/git/commits", payload)
    print("  commit:", commit["sha"][:10])

    # ④ ref
    if head:
        call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": commit["sha"]})
    else:
        call("POST", f"/repos/{repo}/git/refs",
             {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    print(f"✅ 推完了：https://github.com/{repo}/tree/{branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
