# `living_space.py` 住宅空间设计

本文档描述 **`infinigen_examples/constraints/living_space.py`** 中六种住宅语义（卧室 / 客厅 / 厨房 / 饭厅 / 洗手间）及 **`homestudio`** 的**面积档位与家具规则**。  
**全局约定**（`room_area` 公式、共享约束、顶灯、墙净空、灯光倍率、相机、材质）：见 [GLOBAL_DESIGN.md](./GLOBAL_DESIGN.md)。

> **代码入口**：`furniture_constraints(room_type, size_override=...)` → `shared["room_area"]` → 各 `_xxx_constraints`。  
> **距离**：门洞净空等为网格表面最短距离（FCL / `min_dist`）。

---

# 设计原则

本节收录**设计定义**：档位、数量区间、管线分工与源码索引；与「为何如此改、踩坑」无关的内容均放在此处。

---

## 1. `core.py` 中的住宅尺寸档位

住宅六类房型在 `ROOM_SIZE_TIERS` 中的定义与 **`generation/generate_lib/core.py`** 源码一致。档位由脚本内部解析（**不出现在输出目录名**）：

| 行为 | 说明 |
|------|------|
| **bedroom / living-room** | 每个场景在 **small / medium** 两档间按 seed **50%** 随机 |
| **homestudio** | 房型即大单间，固定 **studio** 档（原 large 卧室 / ≥36 m² 家具语义） |
| **kitchen / bathroom / dining-room** | 各房型仅 **medium** 一档（脚本内部写入） |

### 1.1 宽高范围 `(min_w, max_w, min_d, max_d)`（米）

| `room-type` | 档位 | `(min_w, max_w, min_d, max_d)` | 平均面积约（理解 `room_area` 用） |
|-------------|------|--------------------------------|-----------------------------------|
| **bedroom** | small | 3.0–3.9 × 3.0–3.9 | ≈12 m² → **< 16** → 单人床档 |
| **bedroom** | medium | 4.0–5.5 × 4.0–5.5 | ≈20–30 m² → **16–36** → 双人床档 |
| **homestudio** | studio | 5.0–7.0 × 5.0–7.0 | **≥ 36** 平均 → 大卧室 / home-studio 家具档 |
| **living-room** | small | 3.0–5.0 × 3.0–5.0 | 小客厅 |
| **living-room** | medium | 4.0–8.0 × 4.0–8.0 | 中客厅 |
| **kitchen** | medium | 3.2–5.0 × 3.2–5.0 | 单档 |
| **bathroom** | medium | 2.5–3.2 × 2.5–3.2 | 单档 |
| **dining-room** | medium | 3.5–5.5 × 3.5–5.5 | 单档 |

**卧室 small**：平均面积 < 16 m² → 单人床；**medium** 与 **homestudio** 覆盖双人床及大户型规则（见 §3）。

**无 `size_override` 时**的默认宽高范围见源码 **`ROOM_SIZE_PRESETS`**（与上表独立，为未传 box 档位时的回退）。

---

## 2. 全局共享约束与顶灯

住宅房间与其它 **solver 路径**房间共用 `_shared_constraints`、`_lighting_constraints` 的条目与顶灯分档，**全文见 [GLOBAL_DESIGN.md §5–§5.1](./GLOBAL_DESIGN.md)**。本文不再重复。

---

## 3. 卧室（`Semantics.Bedroom` / `homestudio`）

### 3.1 家具分档（由 `room_area` 决定）

| `room_area` | 床 | 床类型 | 床头柜 | 地毯 | 书桌 | 转椅 | 扶手椅 | 茶几 | 落地储物 | 植物 | 落地灯 | 沙发(靠墙) | 沙发(独立) | 沙发边几 | 电视柜 |
|-------------|-----|--------|--------|------|------|------|--------|------|----------|------|--------|------------|------------|----------|--------|
| **< 16 m²** | 1 | 单人床 | 0–2 | 0–1 | 0–1 | 同书桌 | 0 | 0 | 1–3 | 0 | 0–1 | **0** | **0** | 0 | 0 |
| **16–36 m²** | 1 | 双人床 | 0–2 | 0–1 | 0–1 | 同书桌 | 0 | 0 | 2–5 | 0–1 | 0–1 | **0** | **0–1** | 0 | 0 |
| **≥ 36 m²** | 1 | 双人床 | 1–2 | **0–1** | 0–2 | 同书桌 | 0–2 | 0–1 | 2–6 | 1–2 | 1–4 | **1–2** | **0–1** | 1–2 | 0–1 |

- **`< 16 m²`**：`SingleBedFactory`（单人床）；无植物、无沙发。  
- **`16–36 m²`**：`BedFactory`（双人床）；允许 0–1 个独立沙发。  
- **`≥ 36 m²`**：`BedFactory`；靠墙沙发 1–2、独立沙发 0–1、电视柜 0–1、边几 1–2 等。  
- **所有档**：`bedroom_nightstand` score（weight=10）鼓励床头柜贴床侧 ≤ 0.15 m；储物柜上 `OfficeShelfItem` **≥ 0**（弱约束）。

> **`homestudio`** 房型在 `core` 中固定大平面，使 `room_area` 落入 **≥ 36** 档，从而启用上表第三行规则。

---

## 4. 客厅（`Semantics.LivingRoom`）

### 4.1 书桌 / 转椅（`living_space.py` + `core`）

| `room_area` | 书桌 | 转椅 |
|-------------|------|------|
| **< 15 m²** | 0 | 0 |
| **≥ 15 m²** | **0–1** | **0–1** |

> `home.py` 管线（`generate_irregular.py`）：书桌固定 0–1，不做面积分档。

### 4.2 其它（与面积无关的硬约束区间）

| 项目 | 数量 / 条件 |
|------|-------------|
| 沙发（靠背贴墙） | **1–2**（至少 1 张须靠墙） |
| 沙发（侧贴墙） | 0–1 |
| 沙发（仅落地、无靠背约束） | **0–3** |
| **电视柜** | **1–2** |
| **沙发边几** | **1–3** |
| **茶几** | **1–2**（台面 `OfficeShelfItem` 0–3） |
| **地毯** | **0–1** |
| **落地储物（靠墙）** | **0–6** |
| **电视** | 若 `has_tv=True`：每电视柜 **1** 台 |

另有沙发组合、电视柜与窗、茶几与沙发等 **score**（含 storage、coffeetables、rugs、sidetables 等权重）。

---

## 5. 厨房（`Semantics.Kitchen`）

> **约束文件**：`home.py`（`generate_irregular.py`）；`living_space.py`（`core.py`）。两文件数值应保持一致（维护约定见下 **[关键改动](#关键改动)**）。

| 项目 | 数量 / 条件 |
|------|-------------|
| 靠墙橱柜台面（`KitchenSpaceFactory`） | **1–2** |
| 中岛 | **0–1** |
| **厨房水槽**（`SinkFactory`） | 墙柜台面 **恰好 1** + 中岛 **0–1** |
| 洗碗机 | 0–1 |
| 饮料小冰箱 | 0–1 |
| **烤箱** | **恰好 1** |
| 微波炉（相对墙柜） | 0–1 |
| 岛台台面展示物 | 0–4 / 岛 |
| 台面 `KitchenCounterItem`、食品柜上物品 | ≥ 0（弱） |
| **地毯** | **0**（`kitchen_rugs`） |

---

## 6. 饭厅（`Semantics.DiningRoom`）

| 项目 | 数量 / 条件 |
|------|-------------|
| **餐桌** | **恰好 1** |
| **餐椅**（`front_against` 餐桌） | **2–8**（可靠墙） |
| **落地储物（靠墙）** | **1–3**（`core` 路径下 Storage 候选为 **CellShelf + SingleCabinet**，已去掉书柜） |
| **大盆栽** | **0–3**（饭厅优先于全局 0–1） |
| **落地灯** | **`home.py` 不规则公寓**：0–3（score weight=2）；**`living_space.py` 单间**无此项 |
| **地毯** | **0–1**（score `maximize` weight=**3**） |
| **开放式餐厨：墙柜 `KitchenSpaceFactory`** | **0–1**（score `maximize` weight=**10**，非强制） |
| **中岛 `KitchenIslandFactory`** | **0–1**（与墙柜间距 0.7–3 m 等，同厨房岛台逻辑） |
| **洗碗机 / 饮料冰箱** | 各 **0–1**（score 各 weight=**5**；`dining_appliances_layout` 拉近墙柜） |
| 餐桌上 **展示物 / 餐具** | **强制 0**（无台面水槽、无烤箱 score） |
| 储物上 `OfficeShelfItem` | **0–2** |

**Score 权重对照（相对量级，越大越倾向该项）**：墙柜 10、岛台 6、洗碗机/冰箱各 5、落地储物 6、地毯 3、绿植 2；餐桌贴墙/对齐/稳定面 10/10/1。旧版饭厅曾为墙柜 4、水槽/烤箱各 3、落地储物 3（`home`）/ 4（`living_space`），且无地毯 maximize。

---

## 7. 洗手间（`Semantics.Bathroom`）

| 项目 | 数量 / 条件 |
|------|-------------|
| **镜子**（贴墙） | **1** |
| **镜子 ↔ 洗手盆** | 两物体网格最小表面距离 **小于 0.5 m**（硬约束 `bathroom_mirror_sink`，避免镜子在马桶正上方而盆在别处） |
| **立柱 / 立式洗手盆** | **1** |
| **马桶** | **1** |
| **浴室柜**（`SingleCabinetFactory`） | **0–2**（台面仅 `BathroomItem`） |
| **浴缸** | **0–1** |
| **卫浴五金** | **1–4** |
| **挂画** | **0**（仅允许镜子作墙饰） |
| **地毯** | **0**（`bathroom_rugs`） |

洗手间 **不适用** 全局「非浴室 1–7 落地储物」条（见 GLOBAL §5）。

---

## 8. 两条管线的约束文件对照

| 管线 | 脚本 | 约束文件 | 适用房间 |
|------|------|----------|----------|
| 矩形单间 | `core.py` | `living_space.py` | 住宅六类 + homestudio |
| 不规则公寓 | `generate_irregular.py` | **`home.py`** | LivingRoom / DiningRoom / Kitchen（推荐）；Bedroom / Bathroom（可用） |

- **地毯**：单房上限、厨房/浴室零地毯 **在 `living_space.py`（box 管线）** 中实现；`home.py` 以该文件为准。  
- **§5–§7** 硬约束以两处源码同步为准。

---

## 9. 源码索引（住宅）

| 内容 | 文件与位置 |
|------|------------|
| 住宅档位与 `size_override` | `generation/generate_lib/core.py` → `ROOM_SIZE_TIERS`、`resolve_living_space_tier` |
| 分房间约束 | `_bedroom_constraints`, `_livingroom_constraints`, `_kitchen_constraints`, `_bathroom_constraints`, `_diningroom_constraints` |
| 单人床工厂 | `infinigen/assets/objects/seating/bed.py` → `SingleBedFactory` |

---

# 关键改动

本节收录**重大变更说明**与**实现 / 排障要点**（与「设计表里规定的数值」区分开）。改约束时以仓库内代码为准。

## 管线与共存

- **`core.py` + `living_space.py`**：矩形单间、地毯上限与厨房/浴室零地毯等 **仅在此路径完整实现**；文档 **[设计原则](#设计原则)** 中的表格默认指该路径。
- **`home.py`**：不规则多房间公寓；与 `living_space.py` 中厨房 / 饭厅 / 客厅等 **数值应对齐**，否则同一语义在两套管线会不一致。

## 洗手间（镜子）

- **`bathroom_mirror_sink`**：`mirror.distance(sink) < 0.5`（m），与 **`distance(sink).minimize`** 一起把镜子拉向洗手盆一侧；阈值过小在极窄浴室可能更难满足，可改源码常量。

## 厨房

- **`KitchenCabinetFactory`** 已从 `Semantics.Storage` 与相关约束中移除（易穿墙、易出现在非厨房）；厨房储物以 `KitchenSpaceFactory` / 岛台等为准。

## 地毯（`RugFactory`）

- 单房硬上限依赖 **`constraints["rugs_max_per_room"]`**（与各房型 `in_range(0,1)` 一致）。
- **约束图里** `rugs` 必须使用 **`related_to(rooms, cu.on_floor)`**：若写成裸的 `related_to(rooms)`，域会变成 `AnyRelation()`，`RugFactory` 的 Bound 会同时落入多个 greedy stage，触发 `check_problem_greedy_coverage`：**Greedy stages must be non-overlapping**。

## 饭厅（`living_space` 单间）

- **`_living_space_filter_dining_storage`**：饭厅从 `Semantics.Storage` / `AssetPlaceholderForChildren` / `Furniture` 中去掉 **`SimpleBookcaseFactory`**，与 **`_living_space_filter_large_shelf_factory`**（饭厅本就不含 `LargeShelfFactory`）一起，使落地储物以 **CellShelf、SingleCabinet** 为主。

## 储物与大型书架

- **`LargeShelfFactory`** 体积大；住宅 **`furniture_constraints`** 中可按房型 / 预估面积从 `Semantics.Storage` 等用法中剔除，避免小卧室仍随机到大货架（**图书馆**程序化网格仍直接使用 `LibraryLargeShelfFactory`，不受此条影响）。
- 全局 **`furniture_non_penetration`**（若启用）只约束 **`Semantics.Furniture`**，与地毯等无家具语义的物体无关。

## 灯具

- **`constraints["lighting"]`** 针对房间内 **`Semantics.Lighting`**（落地灯、台灯等由求解器放置的对象）；**程序化 AREA 顶灯**通常不在该约束图内。
- **`mesh_ceiling=False`**（住宅顶灯改为 AREA）时，灯具间距与数量需与 `room_area`、各档灯数上限一并考虑，否则易出现无解或后续阶段不稳定。

## 其它

- **`Semantics.HomeStudio`**：大平层卧室档，语义与 `Semantics.Bedroom` 区分，由 `core` 的 `room-type` 与 `ROOM_SIZE_TIERS["homestudio"]` 固定大平面触发 ≥36 m² 家具规则。
- 约束域若使用 **`cl.union`** 等组合，需确认 **`constraint_domain` / `check_all`** 是否支持（不支持时需改写为 `excludes` 等等价形式）。
