package com.etyui.seedhelper;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.class_1297;
import net.minecraft.class_1621;
import net.minecraft.class_2246;
import net.minecraft.class_2248;
import net.minecraft.class_2338;
import net.minecraft.class_2680;
import net.minecraft.class_2818;
import net.minecraft.class_2826;
import net.minecraft.class_310;
import net.minecraft.class_638;
import net.minecraft.class_746;
import net.minecraft.class_4543;

import java.io.BufferedWriter;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

/**
 * 观测记录器：把破种子需要的数据自动写到 seedhelper-observations.txt
 *
 * 记录内容：
 *   hashed_seed  —— 客户端收到的 sa256 哈希种子（能唯一确定完整种子，用来定高 16 位 + 校验）
 *   end_pillars  —— 末地 10 根黑曜石柱的顶面 Y 与反查出的 16 位 v
 *   slime:cx,cz  —— y<40 看到史莱姆的区块（史莱姆区块）
 *   biome:x,z    —— 玩家所在群系采样
 *
 * 注意：1.21.10 的客户端**收不到结构数据**（区块包只有高度图 + 方块实体），
 *       所以结构位置没法像老版本那样自动抓；用规模小的约束（史莱姆区块 / 手动记的结构坐标）凑够就行。
 *
 * 所有逻辑都在客户端主线程（tick）里跑，并且整个包在 try/catch 里，绝不把游戏搞崩。
 */
public class SeedHelperClient implements ClientModInitializer {
    private static final int PILLARS = 10;
    private static final int BASE_TOP_Y = 76;
    private static final int TOP_STEP = 3;

    /** 找末地传送门：在玩家周围这些区块里翻"末地传送门框架"方块 */
    private static final int PORTAL_SCAN_WINDOW = 8;      // 半径（区块）
    private static final int PORTAL_CHUNKS_PER_PASS = 16; // 每次扫多少区块（每秒一次）

    // 原版 EndSpikeFeature 里 10 根柱子的中心坐标：floor(42*cos(2*(-PI+0.3141592653589793*i)))
    private static final int[] PILLAR_X = new int[PILLARS];
    private static final int[] PILLAR_Z = new int[PILLARS];

    private static final long MULT = 0x5DEECE66DL;
    private static final long ADD = 0xBL;
    private static final long MASK = (1L << 48) - 1;

    static {
        for (int i = 0; i < PILLARS; i++) {
            double angle = 2.0 * (-Math.PI + 0.3141592653589793 * i);
            PILLAR_X[i] = (int) Math.floor(42.0 * Math.cos(angle));
            PILLAR_Z[i] = (int) Math.floor(42.0 * Math.sin(angle));
        }
    }

    private BufferedWriter out;
    private final Set<String> written = new LinkedHashSet<>();
    private final Map<Long, Integer> slimeCount = new LinkedHashMap<>();
    private final Map<Long, Integer> lastSlimeReported = new HashMap<>();
    private int ticks;
    private long lastBiomeSampleX = Long.MIN_VALUE;
    private long lastBiomeSampleZ = Long.MIN_VALUE;
    private int biomeSampleTicks;
    private int portalScanCursor;
    private int portalFoundTicks;
    private boolean portalReported;
    private Field biomeAccessSeedField;
    private Object biomeAccessSeedOwner;
    private long biomeAccessSeed;
    private boolean biomeAccessSeedRead;

    @Override
    public void onInitializeClient() {
        try {
            Path gameDir = FabricLoader.getInstance().getGameDir();
            Path file = gameDir.resolve("seedhelper-observations.txt");
            boolean fresh = !Files.exists(file);
            out = Files.newBufferedWriter(file, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
            if (fresh) {
                writeLine("# seedhelper 观测记录 | 直接把这个文件喂给 SeedCracker: java SeedCracker file <路径>");
            }
            System.out.println("[seedhelper] 观测文件: " + file);
        } catch (Throwable t) {
            System.out.println("[seedhelper] 初始化失败: " + t);
        }
        ClientTickEvents.END_CLIENT_TICK.register(this::onTick);
    }

    private void onTick(class_310 client) {
        try {
            class_638 world = client.field_1687;
            class_746 player = client.field_1724;
            if (world == null || player == null || out == null) {
                return;
            }
            ticks++;
            if (ticks % 20 != 0) {
                return;
            }
            String dimension = world.method_27983().method_29177().toString();
            long hashed = hashedSeed(world);
            if (hashed != 0L) {
                writeOnce("hashed_seed=" + hashed);
            }
            if ("minecraft:the_end".equals(dimension)) {
                scanEndPillars(world);
            }
            scanSlimes(world);
            scanBiome(world, player);
            scanPortalFrames(world, player);
        } catch (Throwable t) {
            // 绝不影响游戏
        }
    }

    // ------------------------------------------------------------ 哈希种子

    /** 客户端拿不到原始种子，但 World 里的 BiomeAccess 存着 sha256(seed)，能唯一确定种子 */
    private long hashedSeed(class_638 world) {
        try {
            class_4543 access = world.method_22385();
            if (access == null) {
                return 0L;
            }
            if (biomeAccessSeedField == null || biomeAccessSeedOwner != access) {
                Field field = class_4543.class.getDeclaredField("field_20641");
                field.setAccessible(true);
                biomeAccessSeedField = field;
            }
            biomeAccessSeedOwner = access;
            biomeAccessSeed = biomeAccessSeedField.getLong(access);
            biomeAccessSeedRead = true;
            return biomeAccessSeed;
        } catch (Throwable t) {
            return biomeAccessSeedRead ? biomeAccessSeed : 0L;
        }
    }

    // ------------------------------------------------------------ 末地柱子

    private void scanEndPillars(class_638 world) {
        int[] tops = new int[PILLARS];
        for (int i = 0; i < PILLARS; i++) {
            int top = topPillarBlock(world, PILLAR_X[i], PILLAR_Z[i]);
            if (top < 0) {
                return;   // 还没加载到柱子的区块
            }
            tops[i] = top;
        }
        int[] heights = new int[PILLARS];
        boolean[] used = new boolean[10];
        for (int i = 0; i < PILLARS; i++) {
            int d = tops[i] - BASE_TOP_Y;
            if (d < 0 || d % TOP_STEP != 0) {
                return;
            }
            int h = d / TOP_STEP;
            if (h > 9 || used[h]) {
                return;
            }
            used[h] = true;
            heights[i] = h;
        }
        int value = matchSpikeValue(heights);
        if (value < 0) {
            return;
        }
        StringBuilder sb = new StringBuilder("end_pillars tops=");
        for (int i = 0; i < PILLARS; i++) {
            sb.append(tops[i]).append(i < PILLARS - 1 ? "," : "");
        }
        sb.append(" v=").append(value);
        writeOnce(sb.toString());
    }

    /** 返回该柱子最上面那块黑曜石/基岩的 y（柱子中心列） */
    private int topPillarBlock(class_638 world, int x, int z) {
        class_2818 chunk = world.method_2935().method_21730(x >> 4, z >> 4);
        if (chunk == null) {
            return -1;
        }
        class_2826[] sections = chunk.method_12006();
        if (sections == null) {
            return -1;
        }
        int bottom = worldBottom(world);
        for (int i = sections.length - 1; i >= 0; i--) {
            class_2826 section = sections[i];
            if (section == null) {
                continue;
            }
            for (int localY = 15; localY >= 0; localY--) {
                class_2680 state = section.method_12254(x & 15, localY, z & 15);
                class_2248 block = state.method_26204();
                if (block == class_2246.field_10540 || block == class_2246.field_9987) {
                    return bottom + (i << 4) + localY;
                }
            }
        }
        return -1;
    }

    /** 用 65536 张布局表反查 v（和破解器/原版算法一致） */
    private static int matchSpikeValue(int[] heights) {
        int[] a = new int[PILLARS];
        for (int v = 0; v < 65536; v++) {
            for (int i = 0; i < PILLARS; i++) {
                a[i] = i;
            }
            Lcg random = new Lcg(v);
            for (int i = 10; i > 1; i--) {
                int j = random.nextInt(i);
                int tmp = a[j];
                a[j] = a[i - 1];
                a[i - 1] = tmp;
            }
            boolean ok = true;
            for (int i = 0; i < PILLARS && ok; i++) {
                ok = a[i] == heights[i];
            }
            if (ok) {
                return v;
            }
        }
        return -1;
    }

    /** 和原版 CheckedRandom（= java.util.Random）一致 */
    private static final class Lcg {
        private long state;

        Lcg(long seed) {
            state = (seed ^ MULT) & MASK;
        }

        int next(int bits) {
            state = (state * MULT + ADD) & MASK;
            return (int) (state >>> (48 - bits));
        }

        int nextInt(int bound) {
            int r = next(31);
            int m = bound - 1;
            if ((bound & m) == 0) {
                return (int) (((long) bound * r) >> 31);
            }
            int u = r;
            int res = u % bound;
            while (u - res + m < 0) {
                u = next(31);
                res = u % bound;
            }
            return res;
        }
    }

    // ------------------------------------------------------------ 史莱姆

    private void scanSlimes(class_638 world) {
        for (class_1297 entity : world.method_18112()) {
            if (entity == null || entity.getClass() != class_1621.class) {
                continue;
            }
            double y = entity.method_23318();
            if (y >= 40.0) {
                continue;
            }
            int cx = (int) Math.floor(entity.method_23317()) >> 4;
            int cz = (int) Math.floor(entity.method_23321()) >> 4;
            long key = (((long) cx) << 32) ^ (cz & 0xFFFFFFFFL);
            int count = slimeCount.merge(key, 1, Integer::sum);
            Integer reported = lastSlimeReported.get(key);
            if (reported == null || count >= reported + 2) {
                lastSlimeReported.put(key, count);
                writeLine("slime:" + cx + "," + cz + " count=" + count);
            }
        }
    }

    // ------------------------------------------------------------ 群系采样

    private void scanBiome(class_638 world, class_746 player) {
        class_2338 pos = player.method_24515();
        int x = pos.method_10263();
        int z = pos.method_10260();
        boolean moved = Math.abs(x - lastBiomeSampleX) + Math.abs(z - lastBiomeSampleZ) > 64;
        biomeSampleTicks++;
        if (!moved && biomeSampleTicks < 600) {
            return;
        }
        biomeSampleTicks = 0;
        lastBiomeSampleX = x;
        lastBiomeSampleZ = z;
        try {
            // 这里全走反射：不同版本 method_23753 的返回类型不一样
            //   · 1.19+ 返回 RegistryEntry，要 method_40230() 拿 RegistryKey
            //   · 1.16/1.17/1.18 直接返回 Biome，得去注册表里查名字
            // 写死 class_6880 的话 1.18 及以前根本编不过，所以这里不碰具体类型。
            String name = biomeNameAt(world, pos);
            if (name != null && !name.isEmpty()) {
                writeOnce("biome:" + x + "," + z + "=" + name);
            }
        } catch (Throwable ignored) {
            // 忽略
        }
    }

    /** 拿某个位置的群系名。返回 "minecraft:plains" 这种；拿不到返回 null */
    private String biomeNameAt(class_638 world, class_2338 pos) {
        Object biome;
        try {
            biome = world.getClass().getMethod("method_23753", class_2338.class).invoke(world, pos);
        } catch (Throwable t) {
            return null;
        }
        if (biome == null) {
            return null;
        }
        // 1.19+：RegistryEntry.method_40230() -> Optional<RegistryKey>
        try {
            Object optional = biome.getClass().getMethod("method_40230").invoke(biome);
            if (optional instanceof java.util.Optional<?> key && key.isPresent()) {
                Object registryKey = key.get();
                Object value = registryKey.getClass().getMethod("method_29177").invoke(registryKey);
                if (value != null) {
                    return keyToName(value.toString());
                }
            }
        } catch (Throwable ignored) {
            // 老版本没这个方法，往下走
        }
        // 老版本：去群系注册表里反查这个名字
        try {
            Object registry = biomeRegistry(world);
            if (registry != null) {
                for (String m : new String[]{"method_10221", "method_47956", "method_40302"}) {
                    try {
                        Object id = registry.getClass().getMethod(m, Object.class).invoke(registry, biome);
                        if (id != null) {
                            return keyToName(id.toString());
                        }
                    } catch (Throwable ignored) {
                        // 换下一个方法名
                    }
                }
            }
        } catch (Throwable ignored) {
            // 实在拿不到就算了
        }
        return null;
    }

    /** 从世界对象里掏群系注册表（名字在各版本里也不太一样，只能挨个试） */
    private Object biomeRegistry(class_638 world) {
        try {
            Object manager = world.getClass().getMethod("method_30349").invoke(world);
            for (String keyField : new String[]{"field_11157", "field_41177"}) {
                Class<?> c = manager.getClass();
                while (c != null) {
                    try {
                        java.lang.reflect.Field f = c.getDeclaredField(keyField);
                        f.setAccessible(true);
                        Object key = f.get(null);
                        for (String m : new String[]{"method_30530", "method_10210"}) {
                            try {
                                Object reg = manager.getClass()
                                        .getMethod(m, Class.forName("net.minecraft.class_5321"))
                                        .invoke(manager, key);
                                if (reg != null) {
                                    return reg;
                                }
                            } catch (Throwable ignored) {
                                // 换下一个
                            }
                        }
                    } catch (Throwable ignored) {
                        // 这个字段不在这个类里
                    }
                    c = c.getSuperclass();
                }
            }
        } catch (Throwable ignored) {
            // 拿不到就算了
        }
        return null;
    }

    /** "minecraft:plains" 去掉命名空间前缀，和以前的写法保持一致 */
    private String keyToName(String raw) {
        int i = raw.indexOf(':');
        String s = i >= 0 ? raw.substring(i + 1) : raw;
        int j = s.indexOf(']');
        return j >= 0 ? s.substring(0, j) : s;
    }

    /** 世界最低层。1.17 才有 method_31607()，更老的版本世界底就是 0 */
    private int worldBottom(class_638 world) {
        try {
            Object v = world.getClass().getMethod("method_31607").invoke(world);
            if (v instanceof Integer i) {
                return i;
            }
        } catch (Throwable ignored) {
            // 老版本：没有这个方法
        }
        return 0;
    }

    /** 这个区块段是不是空的。方法名各版本不一样，挨个试，试不出来就当非空（宁可多扫） */
    private boolean sectionEmpty(Object section) {
        for (String m : new String[]{"method_38292", "method_12223", "method_12224"}) {
            try {
                Object v = section.getClass().getMethod(m).invoke(section);
                if (v instanceof Boolean b) {
                    return b;
                }
            } catch (Throwable ignored) {
                // 换下一个
            }
        }
        return false;
    }

    // ------------------------------------------------------------ 写文件

    // -------------------------------------------------- 末地传送门房间

    /** 不用末影之眼也能定位传送门房间：直接翻方块找 end_portal_frame */
    private void scanPortalFrames(class_638 world, class_746 player) {
        if (portalReported && ticks - portalFoundTicks > 1200) {
            return;   // 找到之后再扫 1 分钟就停，省性能
        }
        class_2338 pos = player.method_24515();
        int centerX = pos.method_10263() >> 4;
        int centerZ = pos.method_10260() >> 4;
        int side = PORTAL_SCAN_WINDOW * 2 + 1;
        int total = side * side;
        int bottom = worldBottom(world);
        for (int n = 0; n < PORTAL_CHUNKS_PER_PASS; n++) {
            int index = (portalScanCursor + n) % total;
            int cx = centerX - PORTAL_SCAN_WINDOW + (index % side);
            int cz = centerZ - PORTAL_SCAN_WINDOW + (index / side);
            class_2818 chunk = world.method_2935().method_21730(cx, cz);
            if (chunk == null) {
                continue;
            }
            class_2826[] sections = chunk.method_12006();
            if (sections == null) {
                continue;
            }
            for (int i = 0; i < sections.length; i++) {
                class_2826 section = sections[i];
                if (section == null || sectionEmpty(section)) {
                    continue;
                }
                for (int y = 0; y < 16; y++) {
                    for (int z = 0; z < 16; z++) {
                        for (int x = 0; x < 16; x++) {
                            class_2248 block = section.method_12254(x, y, z).method_26204();
                            if (block == class_2246.field_10398) {          // end_portal_frame
                                int wx = cx * 16 + x;
                                int wy = bottom + (i << 4) + y;
                                int wz = cz * 16 + z;
                                writeOnce("portal_frame:" + wx + "," + wy + "," + wz);
                                if (!portalReported) {
                                    portalReported = true;
                                    portalFoundTicks = ticks;
                                    chat("找到末地传送门框架了！坐标 " + wx + " " + wy + " " + wz);
                                } else {
                                    portalFoundTicks = ticks;
                                }
                            } else if (block == class_2246.field_10027) {   // end_portal（说明已经激活过）
                                writeOnce("portal_lit:" + (cx * 16 + x) + "," + (bottom + (i << 4) + y)
                                        + "," + (cz * 16 + z));
                            }
                        }
                    }
                }
            }
        }
        portalScanCursor = (portalScanCursor + PORTAL_CHUNKS_PER_PASS) % total;
    }

    private void chat(String message) {
        try {
            class_310 client = class_310.method_1551();
            if (client != null && client.field_1705 != null) {
                client.field_1705.method_1758(net.minecraft.class_2561.method_30163(message), false);
            }
        } catch (Throwable ignored) {
            // 忽略
        }
    }

    private void writeOnce(String line) {
        if (written.add(line)) {
            writeLine(line);
        }
    }

    private synchronized void writeLine(String line) {
        try {
            out.write(line);
            out.newLine();
            out.flush();
            System.out.println("[seedhelper] " + line);
        } catch (Throwable ignored) {
            // 忽略
        }
    }
}
