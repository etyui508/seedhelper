# SeedHelper

一个 Minecraft **客户端** Fabric 模组：在游戏里自动记录「破种子」需要的观测数据，
写进 `.minecraft/seedhelper-observations.txt`，再交给另一半工具去反推世界种子。

它不做任何破坏性的事 —— 不改方块、不发网络包、不写世界文件，只是「看」和「记」。

## 它记什么

| 记的东西 | 格式 | 有什么用 |
|---|---|---|
| `hashed_seed` | 64 位整数 | 客户端能拿到服务器发的哈希种子，用来锁定低位 + 校验 |
| `end_pillars` | 10 根柱子的顶面 Y | 反查末地柱子那 16 位随机数 `v` |
| `slime:cx,cz` | 区块坐标 | 你在 y<40 看到史莱姆的区块 = 史莱姆区块 |
| `biome:x,z` | 群系 id | 群系采样点，用来交叉验证 |

输出文件长这样（一行一条，追加写）：

```
hashed_seed 1234567890123456789
end_pillars 76,79,76,82,...
slime:12,-34
biome:128,-512 minecraft:plains
```

## 为什么需要它

纯客户端能拿到的数据很少 —— 服务器不告诉你在哪儿有村庄、要塞。所以破种子的思路是：
收集一堆**小约束**（史莱姆区块、末地柱子高度、群系采样），再让搜索器去撞种子。
这些东西手动记要记到崩溃，这个模组负责自动记。

配套的破解工具在 [mc-seed-toolkit](https://github.com/etyui508/mc-seed-toolkit)。

## 安装

1. 装 [Fabric Loader](https://fabricmc.net/use/)（对应你的 MC 版本）
2. 往 `mods/` 里放 **Fabric API**
3. 把编好的 `seedhelper-1.1.0+<版本>.jar` 也丢进 `mods/`
4. 进游戏（进服务器也行）飞一圈，回主菜单，观察文件就在 `.minecraft/` 里

## 自己编译

不用 Gradle、不用 Loom，全走官方源（Mojang 版本清单 + `maven.fabricmc.net`）：

```bash
python3 build.py 1.21.10          # 编一个版本
python3 build.py 1.21.10 1.20.6   # 编多个
python3 build.py --list           # 看看有哪些版本
```

脚本会自动：下该版本的客户端 jar → 拿 intermediary 映射 → 用 tiny-remapper
把客户端 remap 成 `client-intermediary.jar` → 下 fabric-api 和 fabric-loader →
`javac` 编译 → 输出 `build/seedhelper-1.1.0+<版本>.jar`。

首次跑要下几百 MB（缓存在 `.build-cache/`，之后复用）。需要本机有 JDK。

## 支持版本

源码里写的是 **intermediary** 名字（`class_310`、`method_10263` 这种），
这套名字跨版本是稳定的，所以**同一份源码 1.16.5 ~ 1.21.11 都能编**。

`26.x` 之后官方 jar 不再混淆，也就没有 intermediary 映射了，这份源码编不了；
`build.py` 会明确报错，不会给你一个编坏的包。

## 代码里几个值得说的坑

- **整个 tick 逻辑包在 try/catch 里**，任何异常都只记一行日志，绝不把游戏搞崩。
- **群系、世界最低层、区块段是否为空这些都用反射挨个方法名试** ——
  因为不同版本的方法名和返回类型都不一样，写死一个名字就会在别的版本上炸。
- **1.21.10 的客户端收不到结构数据**（区块包只有高度图 + 方块实体），
  所以这个版本没法像老版本那样自动抓村庄/要塞坐标，只能靠史莱姆区块之类的约束凑。

## 目录

```
src/com/etyui/seedhelper/SeedHelperClient.java   全部逻辑
fabric.mod.json                                  模组元数据（id / 入口点 / 版本依赖）
build.py                                         跨版本构建脚本
tools/gh-push-api.py                             往 GitHub 推文件的小工具（走 API）
```

## 许可

MIT，见 [LICENSE](LICENSE)。
