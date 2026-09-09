# apk16k-check

APK 16KB 分页兼容性检查工具：检查 APK 内 `.so` 是否满足 Android 16 的 16KB 分页要求（ZIP 容器对齐 + ELF 段对齐双层判定）。

Android 16 (Vanilla Ice Cream) 将系统内存页大小从 **4KB 提升到 16KB**。本工具提供零依赖的 Python 检查，按 Android 官方规则对任意 APK 做**双层** 16KB 兼容性检查。

## 为什么需要查两层？

`.so` 本质上是 ELF 文件，但它"住在" APK 这个 ZIP 容器里。Android 加载 native 库时，为了性能，对**未压缩（stored）**的 `.so` 会直接在 APK 内部做 `mmap` 映射，而不是先解压成独立文件。这就引入了两个独立的对齐要求：

```
① ZIP 容器层  : .so 条目在 APK 文件内的数据偏移必须 16K 对齐
                → 保证 mmap 的起点是正的

② ELF 文件层  : .so 每个 PT_LOAD 段的 p_align >= 16384，
                 且 p_offset % 16K == p_vaddr % 16K
                → 保证映射后各段正好落在链接器声明的虚拟地址上

①②都满足 → 系统可直接 mmap(APK, 偏移, 虚拟地址) 一步到位，零拷贝零错位
```

- **ZIP 层为什么必须对齐**：内核 `mmap` 只能按页的整数倍偏移建立映射。若 .so 在 APK 里的起始偏移不是 16K 对齐（旧打包工具只按 4K 对齐），映射起点就是"歪"的，和链接器期望的虚拟地址对不上。
- **ELF 层为什么必须对齐**：文件偏移的页对齐起点决定虚拟地址的页对齐起点，两者错位则段的页边界对不上内容。`p_align>=16384` 是段的"对齐声明"，4K 时代编译的旧库写的是 4096，不达标。

## 压缩条目（deflated）是怎么回事？

**压缩与否由打包 APK 的应用开发者决定，与 .so 编译者无关**——编译器产出的 .so 是独立文件，从不"压缩"；压缩发生在把它塞进 ZIP 容器时，由构建配置决定：

```
android:extractNativeLibs="false"  → .so 以 stored（未压缩）进 APK
                                     系统可直接在 APK 内 mmap → 需要 ①② 全对齐

android:extractNativeLibs="true"   → .so 以 deflated（压缩）进 APK
                                     安装时系统解压成独立文件 → 偏移天然页对齐，
                                     ZIP 层要求豁免，但 ELF 层要求依旧存在
```

开发者常混淆的一点：**"压缩豁免 ZIP 要求"不是免费的**，代价是：

1. 运行时多一次解压开销，App 冷启动变慢
2. APK 压缩体积 + 解压出的完整 .so 副本，双份存储
3. Google Play 早就不推荐压缩原生库（性能方向是直接映射）

因此主流方向是 **stored + 16K 对齐**，而不是压缩豁免。"重打包"（`zipalign -P 16`）做的事是：**让 .so 保持未压缩、同时把它在 APK 内的偏移对齐到 16K**，兼顾性能与兼容性——它不是把库压缩，而是保住"不压缩"这个优点并补齐对齐。

## 判定标准

| 层 | 规则 | 不达标的处理 |
|----|------|-------------|
| ZIP 容器层 | stored 的 `.so` 条目数据偏移为 16384 整数倍 | 重打包：`zipalign -P 16` |
| ELF 文件层 | 每个 PT_LOAD 段 `p_align >= 16384` 且 `p_offset % 16384 == p_vaddr % 16384` | 重编译：NDK r27+ 或联系 SDK 提供方 |

非 ELF 的 `.so`（伪装成库的数据文件）无需重编译，但作为 stored 条目仍需随整体重打包。

deflated（压缩）条目豁免 ZIP 层检查，**但不能豁免 ELF 层**——解压后仍是需被 linker 加载的 .so。

## 工具

### 16kb_check.py

Python 3.6+，仅标准库，无第三方依赖。

```bash
python 16kb_check.py <xxx.apk>             # 完整详情（ZIP 逐项偏移 + ELF 逐库判定）+ 汇总表
python 16kb_check.py <xxx.apk> --summary   # 只输出汇总表
```

输出示例（汇总表）：

```
  library                                                    ZIP16K    ELF16K    结论
  lib/arm64-v8a/libaaa.so                                    BAD       BAD       重打包 + 重编译
  lib/arm64-v8a/libbbb.so                                    ok        BAD       重编译 (p_align<16K)
  lib/arm64-v8a/libccc.so                                    BAD       ok        重打包 (zipalign -P 16)
  lib/arm64-v8a/libddd.so                                    ok        ok        兼容
```

### 官方工具交叉验证

```bash
# 16KB 对齐检查（-P 16 表示 16KB 页；最后的 4 是传统 4 字节通货对齐）
zipalign -c -P 16 -v 4 <xxx.apk>
```

两者判定一致：`16kb_check.py` 的 ZIP 层基于解析本地文件头计算真实偏移，与官方 zipalign 算法等价。

## 修复流程

```bash
# 1. 重编译 p_align 不足的 .so（自研：NDK r27+；第三方：联系提供方）
#    常见做法：-Wl,-z,max-page-size=16384
# 2. 将新 .so 替换进 APK（用 zip 工具）
# 3. 16KB 重打包
zipalign -f -P 16 4 input.apk aligned.apk
# 4. 复验：应无 BAD 条目
zipalign -c -P 16 -v 4 aligned.apk
python 16kb_check.py aligned.apk --summary
# 5. 重新签名（zipalign 会破坏原签名）
apksigner sign --ks your.keystore --out final.apk aligned.apk
# 6. 真机验收：16KB 设备/模拟器上安装后无兼容性弹窗
```

## 参考资料

- [Android 16KB 页面大小兼容性](https://developer.android.com/guide/practices/page-sizes)
- [NDK 16KB 页面支持](https://developer.android.com/ndk/guides/16kb-page-size)
- [ELF 规范](https://refspecs.linuxfoundation.org/elf/elf.pdf)