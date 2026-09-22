# 把 Apple Watch 的数据接进训练程序：调研与方案

> 这是一份**调研笔记**，不是代码。目的是先把"苹果这边到底能给出什么"搞清楚，
> 再决定做什么功能。结论部分已经用真机事实核对过，未能核实的都标了
> 「**待实测**」。

---

## 0. 结论速览

| 问题 | 答案 |
|---|---|
| 苹果手表能直接通过蓝牙连电脑、被电脑读吗？ | **不能。** 苹果不允许手表 App 向外部设备广播蓝牙信号，iPhone 必须夹在中间当"蓝牙桥" |
| 那电脑能读到什么？ | **只有实时心率（BPM）一个数**，走标准蓝牙心率服务 `0x180D` |
| GymKit（苹果官方的健身器材方案）能用吗？ | **不能。** 需要 NFC 握手 + MFI 认证芯片，第三方无法实现 |
| HRV、静息心率、睡眠、VO2max、活动环、卡路里呢？ | 这些只在 **HealthKit**（iPhone 侧），蓝牙拿不到，只能**事后同步** |
| 现在的程序缺什么？ | 完全没有标准心率服务客户端；报告里也没有任何心率字段（见 §5） |

---

## 1. 苹果这边的三条硬限制

### 1.1 GymKit 这条路是封死的

GymKit 是苹果给商用健身器材的官方方案（跑步机/动感单车把数据直接写进手表）。
[apple-gymkit](https://github.com/kormax/apple-gymkit) 的反编译研究显示：配对必须先做
**NFC ECP 握手**，再由健身器材用**苹果 MFI 认证芯片**做挑战应答，之后才是 BLE GATT
通信。研究者直接停在配对这一步，原话是"信息非常有限，不足以实现该规范"。

→ **排除 GymKit。** 我们没有 MFI 芯片，也不该往这个方向走。

### 1.2 手表不能作为蓝牙外设广播

这条最反直觉，也是最关键的一条。做这件事的商业 App
[HeartCast 的官方 FAQ](https://www.heartcast.app/faq-help-support-issues/)（2025-12 更新）
写得很直接：

> Apple does not permit apps on Apple Watch to broadcast Bluetooth signals to external
> fitness equipment. The iPhone is required as a "Bluetooth bridge".

也就是说，即便 `CBPeripheralManager` 在 watchOS 上有没有 API 层面的支持还有讨论
（见 [Apple 开发者论坛相关帖](https://developer.apple.com/forums/thread/716929)），
**实际可用的做法就是 iPhone 广播**。市场上所有同类 App（HeartCast、HeartBLE、
[HR Broadcast](https://apps.apple.com/cn/app/hr-broadcast/id6766073249)）都是
"手表测 → iPhone 广播"这个结构，没有一个是手表单独广播的。

> **待实测**：拿一个广播 App 开启后，用蓝牙扫描看能不能搜到一个带 `0x180D` 服务
> 的设备、以及设备名是什么。这一步 5 分钟就能做完，决定后面所有设计。

### 1.3 广播出来的只有 BPM

`0x180D`（Heart Rate Service）里只有一个有意义的特征值 `0x2A37`
（Heart Rate Measurement）。它给的是**当前心率（和可选的 RR 间期）**，
没有卡路里、没有 HRV、没有血氧、没有步频。心率区间要接收端自己按
最大心率 / 储备心率去算。

好消息是：**它是标准服务**，任何 BLE central 都能订阅，不需要配对、不需要认证。
HeartCast 明确列出了在**电脑**上验证可用的接收端（Zwift、TacX、ROUVY 等），
说明"Mac 当接收端"这条路是成熟的。

---

## 2. 可行链路

```
Apple Watch  ──(HealthKit，必须处于一次 workout 中)──▶  iPhone
                                                          │ 第三方 App 用 0x180D 广播
                                                          ▼
                                              电脑（本程序，BLE central 订阅 0x180D）
```

使用时的几个坑（都来自 HeartCast 的 FAQ，属于 iOS/HealthKit 层面的通用限制）：

- iPhone 上那个广播 App 在**首次连接时必须保持前台可见**；连上之后可以息屏、可以后台
- 手表**必须处于一次 workout 中**（HealthKit 硬要求），而且**同时只能有一个 workout**
  ——如果你同时在手表上开了别的训练 App，会互相把对方踢掉
- **同一台设备不能既广播又接收**（广播的 iPhone 上跑接收端 App 不行）。
  我们正好是 Mac 接收，不受影响
- 广播是"一对多"的，但实际上一台设备抢到连接之后，别的就连不上了

### 2.1 一个更省事的替代：标准蓝牙心率带

既然用的是标准 `0x180D`，那么**任何标准心率带**（Polar H10、Wahoo TICKR、迈金、
Coospo 之类，几百块）都能被电脑直接读，而且：

| | Apple Watch + iPhone 桥 | 蓝牙心率带 |
|---|---|---|
| 需要 iPhone 在场 | 要 | 不要 |
| 需要手表开 workout | 要（会占用手表的训练记录） | 不要 |
| 首次连接要 App 前台 | 要 | 不要 |
| 数据质量 | 光学，滞后几秒，运动时易受干扰 | 心电，通常更准更快 |
| 额外成本 | 0（已有手表） | 几百块 |

→ 建议：**程序按标准 `0x180D` 做，两种设备都支持**。手表方案作为"不想再买设备"的
备选，心率带作为推荐方案。

---

## 3. 心率以外的数据：只能事后同步

HRV、静息心率、睡眠时长、VO2max、活动环、总卡路里 —— 这些**没有 BLE 通道**，
只有 HealthKit。要进电脑，三条路：

1. **iOS 快捷指令（Shortcuts）**：定时"获取健康样本"→ 写成文件到 iCloud Drive，
   或者直接 POST 到本程序的 HTTP 接口。不用写 App，但配置略绕
2. **专门的导出 App**，如 [Health Auto Export](https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069)：
   支持定时导出 JSON/CSV，也支持推送到自定义 REST 端点。本程序已经有 aiohttp 服务，
   在局域网里多暴露一个接收接口即可，是最省事的一条路
3. 自己写一个 iOS App（最重，不建议）

**而且都是非实时的**：一天一次或训练后一次。数据本身的产生频率也有限——
HRV 主要在睡眠和正念时测，VO2max 只在外出步行/跑步时更新，静息心率一天一个值。
这些都不是"训练进行中"能用的量。

> 参考：社区里已经有把 HealthKit 数据取出来给别的程序用的实现，例如
> [apple-health-mcp](https://github.com/alphonsekoh/apple-health-mcp)（本地 MCP 服务）。

---

## 4. 现在程序里的实际情况（已核对代码）

| 能力 | 现状 |
|---|---|
| 解析 FTMS `Indoor Bike Data` 里的心率字段 | **有**（`ftms.py` 的 `heart_rate_bpm`） |
| 界面显示实时心率 | **有**（`mHr` 那一格） |
| 标准心率服务 `0x180D` 客户端 | **完全没有** |
| 心率写进每秒轨迹（trace） | **没有** |
| 训练报告里有任何心率字段 | **没有**（avg/max/曲线/区间都没有） |

所以现状是：**心率这条路只在你那台骑行台自己上报心率时才通，而它不上报。**
独立心率设备接不进来，就算接进来了也留不下数据。

---

## 5. 功能清单（按价值排序）

### 第一层：把心率先接进来（必要基础）

1. **标准 `0x180D` 客户端**：扫描 / 连接 / 订阅任意标准心率设备（手表桥或心率带），
   断线自动重连。界面上显示实时心率和当前心率区间
2. **心率入库**：每秒进 trace，报告里给出平均心率、最大心率、心率曲线（和功率同图）
3. **心率区间分布**：按 %HRmax 或 Karvonen（储备心率，需要静息心率）分 5 区，
   和现有的功率区间分布并列显示

### 第二层：功率 + 心率一起看（这才是"完善训练"）

4. **解耦率（Pa:HR decoupling）**：把一次训练切成前后两半，比较"功率/心率"比值的变化。
   这是有氧耐力最经典的一个指标，长距离课表尤其有用。我们已经有完整的两段数据，
   本质就是一个比值
5. **心率漂移**：同样功率下心率随时间爬升多少。用来判断"这个强度到底撑不撑得住"
6. **效率因子 EF = NP / 平均心率**，以及跨训练的 EF 趋势曲线。同一个功率心率越来越低
   ＝ 有氧能力在涨
7. **心率区间 vs 功率区间偏差提示**：设的是 Z2 功率但心率一直在 Z4，
   可能是 FTP 设高了、也可能今天状态不好。这一条对"乳酸科学训练"那个模块价值最大
8. **间歇恢复判读**：每组结束时心率多少、结束 60 秒后掉了多少（心率恢复 HRR）。
   HIIT 的分段统计里加一列就够
9. **卡路里双源估算**：现在按 23% 做功效率折算是粗略值；有持续心率后可以用
   Keytel 公式（需要体重/年龄/性别，新增几个设置项）算一个更贴近实际的消耗，
   两个数并列显示，让用户自己判断

### 第三层：需要 HealthKit 事后同步（价值高，但要手机侧配置）

10. **晨起就绪度**：用昨晚的 HRV + 静息心率 + 睡眠时长，在开始训练前给一句
    "今天适合上强度还是轻松骑"，并据此**自动微调**当天间歇的目标功率
11. **长期负荷趋势**：功率侧的训练量 + 心率侧的训练冲量（TRIMP），看一周/一月的
    负荷曲线，避免连续高强度
12. **FTP 交叉验证**：坡道/20 分钟测出的 FTP，和阈值心率是否自洽
13. **异常提示**：当天静息心率明显高于基线时，报告里提示"可能恢复不足"

### 不建议做

- **用心率代替功率做 ERG 控制**。FTMS 里确实有 `Set Target Heart Rate (0x06)`，
  但心率滞后 30~60 秒、受情绪/咖啡因/温度影响，闭环控出来的功率会非常难看。
  可以做**心率上限保护**（超过阈值自动把目标功率降 5%）——那是安全保护，不是控制方式。

---

## 6. 建议的推进顺序

1. **先只做第一层**（独立心率源 + 心率入库 + 报告显示）。这一步不需要任何手机侧改动，
   买根心率带当场就能验证；想用手表方案就装一个广播 App
2. **攒几次训练数据后做第二层**。解耦、漂移、EF 这些指标必须看趋势，
   单次数据说明不了问题
3. **HealthKit 同步放最后**。它需要你在手机上做配置，而且价值要靠前两步的数据积累
   才能体现

---

## 7. 动手前需要先收集/确认的信息

- [ ] **待实测**：Apple Watch 装一个广播 App 后，电脑能不能扫到带 `0x180D` 的设备？
      设备名是什么？能不能订阅到心率？
- [ ] 有没有蓝牙心率带？没有的话愿不愿意买一根（推荐，比手表链路稳）
- [ ] 静态参数：年龄、体重、性别（Keytel 公式要用）
- [ ] 最大心率：是用 `220 - 年龄` 估算，还是有一次实测的最大心率？
- [ ] 静息心率：手表能给（HealthKit），也可以手动填
- [ ] 是否接受在 iPhone 上配置一个导出 App 来送 HRV/睡眠数据

---

## 8. 参考

- [HeartCast 官方 FAQ](https://www.heartcast.app/faq-help-support-issues/) —— 手表不能广播、
  必须 iPhone 做桥、`0x180D`、各家接收端兼容性列表（最实用的一手材料）
- [apple-gymkit（GymKit 反编译研究）](https://github.com/kormax/apple-gymkit) —— 为什么 GymKit 走不通
- [Apple 开发者论坛：Connect a Mac and a Watch via Core Bluetooth](https://developer.apple.com/forums/thread/759288)
- [Apple 开发者论坛：Transmitting data from the Apple watch to a third party device](https://developer.apple.com/forums/thread/26577)
- [Apple 开发者论坛：watchOS 上用 CBPeripheralManager](https://developer.apple.com/forums/thread/716929)
- [Health Auto Export（HealthKit 定时导出/推送）](https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069)
- [apple-health-mcp（HealthKit 数据本地取用）](https://github.com/alphonsekoh/apple-health-mcp)
