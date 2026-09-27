#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SeedHelper —— 从源码构建这个 Fabric 记录模组，不用 Gradle/Loom，
全走官方源（Mojang 版本清单 + maven.fabricmc.net）。

做这些事：
    1) Mojang 版本清单里拿到该版本客户端 jar
    2) maven.fabricmc.net 下该版本的 intermediary 映射
    3) 用 tiny-remapper 把客户端 remap 成 client-intermediary.jar
    4) 下该版本的 fabric-api + fabric-loader
    5) javac 编译
    6) 输出 build/seedhelper-<版本>.jar

源码里写的是 intermediary 名字，所以同一份源码跨版本都能编。
（26.x 之后官方 jar 不再混淆，没有 intermediary 映射，这版源码没法直接编 ——
脚本会明确报错，不会给你一个编坏的包。）

用法:
  python3 build.py 1.21.10 1.21.4 1.20.6 ...
  python3 build.py --list           # 看看有哪些版本

需要本机有 JDK（javac）。旧版本用哪个 JDK 都行；26.x 的类文件是 Java 25 编的，
得用 Java 25 的 javac（可以用 MC_JAVAC=/路径/javac 指定）。
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE
CACHE = os.path.join(ROOT, ".build-cache")
OUTDIR = os.path.join(ROOT, "build")

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_MAVEN = "https://maven.fabricmc.net"
MODRINTH = "https://api.modrinth.com/v2"
FALLBACK_LOADER = "0.19.5"        # meta 站连不上时用的 fabric-loader 版本


def log(*a):
    print(*a, flush=True)


def fetch(url, dest, desc=None, tries=3):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    log(f"    下载 {desc or os.path.basename(dest)} …")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mc-seed-toolkit-build"})
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return dest
        except Exception as e:      # 国内网络偶尔抽风，重试几次
            last = e
            log(f"      （第 {i + 1} 次失败：{e}，重试）")
    raise last


def fetch_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mc-seed-toolkit-build"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:
            last = e
    raise last


def manifest():
    return fetch_json(MOJANG_MANIFEST)


def version_info(man, ver):
    for v in man["versions"]:
        if v["id"] == ver:
            return fetch_json(v["url"])
    return None


# ---------------------------------------------------------------- 工具
def tool_jar(name, url):
    dest = os.path.join(CACHE, "tools", os.path.basename(url))
    return fetch(url, dest, name)


def tiny_remapper():
    ver = "0.10.4"
    return tool_jar("tiny-remapper",
                    f"{FABRIC_MAVEN}/net/fabricmc/tiny-remapper/{ver}/tiny-remapper-{ver}-fat.jar")


# ---------------------------------------------------------------- seedhelper
def _api_looks_complete(jar_path):
    """fabric-api 的 jar 里有没有 META-INF/jars/ 子模块？
    ≤1.18.2 的 maven 上只有空壳（几 KB），真身在 Modrinth。"""
    try:
        if os.path.getsize(jar_path) < 100 * 1024:
            return False
        with zipfile.ZipFile(jar_path) as z:
            return any(n.startswith("META-INF/jars/") and n.endswith(".jar")
                       for n in z.namelist())
    except Exception:
        return False


def fetch_fabric_api_from_modrinth(game_ver, api_ver):
    """从 Modrinth 拿该游戏版本对应的 fabric-api（那个才是带子模块的完整版）"""
    data = fetch_json(f"{MODRINTH}/project/fabric-api/version"
                      f"?game_versions=%5B%22{game_ver}%22%5D&loaders=%5B%22fabric%22%5D")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Modrinth 上找不到 {game_ver} 的 fabric-api")
    files = [f for f in data[0].get("files", []) if f.get("filename", "").endswith(".jar")]
    if not files:
        raise RuntimeError("Modrinth 返回里没有 jar")
    f = files[0]
    out = os.path.join(CACHE, "api", f"modrinth-{game_ver}-{api_ver}.jar")
    return fetch(f["url"], out, f"fabric-api（Modrinth {game_ver}）")


def nearest_api_version(ver):
    """该游戏版本对应的 fabric-api 版本（取 maven 上最新的 +<ver>）"""
    xml = os.path.join(CACHE, "fabric-api-metadata.xml")
    if not os.path.exists(xml):
        fetch(f"{FABRIC_MAVEN}/net/fabricmc/fabric-api/fabric-api/maven-metadata.xml", xml, "fabric-api 版本表")
    import re
    txt = open(xml, encoding="utf-8", errors="replace").read()
    vers = re.findall(r"<version>([^<]+)</version>", txt)
    hit = [v for v in vers if v.endswith("+" + ver)]
    if not hit:
        # 老版本 fabric-api 的版本号是按 "1.16" 这种系列写的（1.16.5 也可能是 +1.16.5）
        parts = ver.split(".")
        if len(parts) >= 3:
            series = ".".join(parts[:2])
            hit = [v for v in vers if v.endswith("+" + series)]
    if not hit:
        return None
    # 取版本号最大的那个
    def key(v):
        import re as _re
        nums = [int(x) for x in _re.findall(r"\d+", v.split("+")[0])]
        return nums
    return sorted(hit, key=key)[-1]


def loader_version(ver):
    """fabric-loader 版本。loader 本身基本不挑游戏版本，meta 站抽风就用固定版本。"""
    try:
        data = fetch_json(f"https://meta.fabricmc.net/v2/versions/loader/{ver}", tries=2)
        for d in data:
            if d["loader"]["stable"]:
                return d["loader"]["version"]
        if data:
            return data[0]["loader"]["version"]
    except Exception as e:
        log(f"    （meta.fabricmc.net 连不上：{e}；用固定 loader 版本）")
    return FALLBACK_LOADER


def download_libraries(info):
    """把这个版本 JSON 里列的游戏依赖库都下下来（编译/remap 都要用）"""
    jars = []
    for lib in info.get("libraries", []):
        name = lib.get("name") or ""
        if "natives" in name:
            continue
        art = (lib.get("downloads") or {}).get("artifact")
        if not art:
            continue
        path = art["path"]
        dest = os.path.join(CACHE, "libs", path.replace("/", os.sep))
        if not os.path.exists(dest):
            try:
                fetch(art["url"], dest, os.path.basename(path))
            except Exception as e:
                log(f"    （跳过 {os.path.basename(path)}: {e}）")
                continue
        jars.append(dest)
    return jars


def build_seedhelper(man, ver, java="java"):
    log(f"  [seedhelper {ver}]")
    info = version_info(man, ver)
    if not info:
        log("    ✗ Mojang 清单里没有这个版本")
        return None
    client = info["downloads"].get("client")
    if not client:
        log("    ✗ 这个版本没有客户端 jar")
        return None
    client_jar = fetch(client["url"], os.path.join(CACHE, "client", f"client-{ver}.jar"), f"客户端 {ver}")
    libs = download_libraries(info)

    # intermediary（26.x 起官方 jar 已不混淆，Fabric 用的是 0.0.0 恒等映射，就不需要 remap 了）
    interl_jar = os.path.join(CACHE, "intermediary", f"intermediary-{ver}-v2.jar")
    try:
        fetch(f"{FABRIC_MAVEN}/net/fabricmc/intermediary/{ver}/intermediary-{ver}-v2.jar", interl_jar,
              f"intermediary {ver}")
        has_intermediary = os.path.getsize(interl_jar) > 200
    except Exception:
        has_intermediary = False

    if has_intermediary:
        with zipfile.ZipFile(interl_jar) as z:
            mapp = [n for n in z.namelist() if n.endswith(".tiny")][0]
            z.extract(mapp, os.path.join(CACHE, "intermediary", ver))
        mappings = os.path.join(CACHE, "intermediary", ver, mapp)
        remapped = os.path.join(CACHE, "intermediary", f"client-intermediary-{ver}.jar")
        if not os.path.exists(remapped):
            log("    用 tiny-remapper 把客户端转成 intermediary 名字 …")
            r = subprocess.run([java, "-jar", tiny_remapper(), client_jar, remapped, mappings,
                                "official", "intermediary"],
                               capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(remapped):
                log("    ✗ remap 失败：" + (r.stderr or r.stdout)[-400:])
                return None
        game_jar = remapped
    else:
        log("    （这版官方 jar 没混淆，不用 remap）")
        game_jar = client_jar

    api_ver = nearest_api_version(ver)
    if not api_ver:
        log(f"    ✗ maven 上没有 +{ver} 的 fabric-api")
        return None
    api_jar = os.path.join(CACHE, "api", f"fabric-api-{api_ver}.jar")
    api_jar = fetch(f"{FABRIC_MAVEN}/net/fabricmc/fabric-api/fabric-api/{api_ver}/fabric-api-{api_ver}.jar",
                    api_jar, f"fabric-api {api_ver}")
    # ≤1.18.2 的 maven 上只有个空壳（几百 KB 那种才是真 fat jar），
    # 真身在 Modrinth。判断标准：没有 META-INF/jars 里的子模块就是空壳。
    if not _api_looks_complete(api_jar):
        log("    （maven 上那份是空壳，改从 Modrinth 拉真正带子模块的）")
        api_jar = fetch_fabric_api_from_modrinth(ver, api_ver)
    ld_ver = loader_version(ver)
    loader_jar = fetch(f"{FABRIC_MAVEN}/net/fabricmc/fabric-loader/{ld_ver}/fabric-loader-{ld_ver}.jar",
                       os.path.join(CACHE, "loader", f"fabric-loader-{ld_ver}.jar"), f"fabric-loader {ld_ver}")

    # fabric-api 里嵌套的子模块要解出来才能编译
    nested = os.path.join(CACHE, "nested", api_ver)
    # 注意：判断"解没解开"要看里面有没有 jar，不能只看目录在不在 ——
    # 以前失败过一次留下了空目录，之后每次都以为已经解好了，永远缺子模块。
    if not glob.glob(os.path.join(nested, "META-INF", "jars", "*.jar")):
        os.makedirs(nested, exist_ok=True)
        with zipfile.ZipFile(api_jar) as z:
            for n in z.namelist():
                if n.startswith("META-INF/jars/") and n.endswith(".jar"):
                    z.extract(n, nested)
        log(f"    解开 fabric-api 的 {len(glob.glob(os.path.join(nested, 'META-INF', 'jars', '*.jar')))} 个子模块")

    if not has_intermediary:
        log("    ✗ 这个版本的官方 jar 不再混淆（没有 intermediary 映射），")
        log("      而 src/ 里写的是 intermediary 名字 —— 需要一份官方名的源码才能编。")
        return None
    src = os.path.join(ROOT, "src")
    srcs = [os.path.join(r, f) for r, _, fs in os.walk(src) for f in fs if f.endswith(".java")]
    classes = os.path.join(CACHE, "classes", f"seedhelper-{ver}")
    if os.path.isdir(classes):
        shutil.rmtree(classes)
    os.makedirs(classes)
    cp = os.pathsep.join([game_jar, loader_jar] + libs +
                         glob.glob(os.path.join(nested, "META-INF", "jars", "*.jar")))
    # 26.x 的类文件是 Java 25 编的，得用 Java 25 的 javac 才读得懂；
    # 别的版本用默认的 javac 就行。（MC_JAVAC 可以指定一个）
    javac = os.environ.get("MC_JAVAC") or "javac"
    r = subprocess.run([ javac, "-encoding", "UTF-8", "-proc:none", "-nowarn",
                         "-cp", cp, "-d", classes] + srcs, capture_output=True, text=True)
    if r.returncode != 0:
        log("    ✗ 编译失败：")
        log("      " + (r.stderr or r.stdout).strip().replace("\n", "\n      ")[:1200])
        return None
    # fabric.mod.json（版本号按目标版本改）
    meta = json.load(open(os.path.join(ROOT, "fabric.mod.json"), encoding="utf-8"))
    meta["depends"]["minecraft"] = ver
    meta["version"] = f"1.1.0+{ver}"
    with open(os.path.join(classes, "fabric.mod.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, f"seedhelper-1.1.0+{ver}.jar")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, fs in os.walk(classes):
            for f in fs:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, classes))
    log(f"    ✓ {os.path.relpath(out, ROOT)}（{os.path.getsize(out)//1024} KB）")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("versions", nargs="*")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    man = manifest()
    if args.list:
        for v in man["versions"][:40]:
            print(f'  {v["id"]:20s} {v["type"]:9s} {v["releaseTime"][:10]}')
        return
    if not args.versions:
        ap.error("给几个版本号，比如 1.21.10 1.20.6")

    ok, bad = [], []
    for ver in args.versions:
        log(f"== {ver}")
        try:
            made = build_seedhelper(man, ver)
        except Exception as e:
            log(f"    ✗ seedhelper 出错：{e}")
            made = None
        (ok if made else bad).append(ver)
    log("")
    log(f"完成: {', '.join(ok) if ok else '（无）'}")
    if bad:
        log(f"失败: {', '.join(bad)}")
    log(f"产物目录: {os.path.relpath(OUTDIR, ROOT)}")


if __name__ == "__main__":
    main()
